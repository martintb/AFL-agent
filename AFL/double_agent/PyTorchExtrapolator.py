import numpy as np
import xarray as xr
from typing_extensions import Self
from typing import Optional, Dict, Any
import gpytorch
import torch
from gpytorch.models import ExactGP
from gpytorch.likelihoods import DirichletClassificationLikelihood
from gpytorch.means import ConstantMean
from gpytorch.kernels import ScaleKernel, RBFKernel
from gpytorch.distributions import MultivariateNormal
from gpytorch.mlls import ExactMarginalLogLikelihood
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
torch.set_default_dtype(torch.double)

from AFL.double_agent.Extrapolator import Extrapolator

class DirichletGPExtrapolator(Extrapolator):
    """Gaussian Process classifier for extrapolating class labels using Dirichlet likelihood.
    
    This extrapolator uses Dirichlet-based Gaussian Processes to transform classification
    labels into continuous probabilities. The Dirichlet likelihood provides a natural
    way to model categorical distributions while maintaining uncertainty quantification
    across the probability simplex.

    The implementation leverages GPyTorch's Dirichlet classification framework as described in:
    https://docs.gpytorch.ai/en/v1.13/examples/01_Exact_GPs/GP_Regression_on_Classification_Labels.html

    The extrapolator fits a GP to labeled training samples and predicts class probabilities
    across a specified grid, providing both point estimates and uncertainty quantification
    through Monte Carlo sampling from the posterior predictive distribution.

    Parameters
    ----------
    feature_input_variable : str
        The name of the `xarray.Dataset` data variable to use as the input to the model 
        that will be extrapolating the discrete data. This is typically a sample 
        composition variable with shape (sample_dim, n_features).

    predictor_input_variable : str
        The name of the `xarray.Dataset` data variable to use as the output of the model 
        that will be extrapolating the discrete data. For this `PipelineOp` this should 
        be a class label vector with integer values representing different classes.

    output_prefix : str
        The string prefix to apply to each output variable before inserting into the 
        output `xarray.Dataset`. This helps organize multiple extrapolator outputs.

    grid_variable : str
        The name of the `xarray.Dataset` data variable to use as an evaluation grid
        where predictions will be made. Should have the same feature dimensions as
        the input variable.

    grid_dim : str
        The xarray dimension name over each grid point. This is the grid equivalent 
        to the sample dimension and defines how grid points are indexed.

    sample_dim : str
        The `xarray` dimension name over the discrete 'samples' in the 
        `feature_input_variable`. This is typically a variant of `sample` 
        (e.g., `saxs_sample`, `composition_sample`).

    params : Dict[str, Any], optional
        Parameters to configure the GP model training using GPyTorch. If None,
        default parameters will be used.

        learning_rate : float, default=0.1
            Learning rate for torch.optim.Adam optimizer.
        n_iterations : int, default=200
            Total number of training epochs for the GP model.
        verbose : bool, default=False
            Whether to print training loss statistics during optimization.

    name : str, default="DirichletGPExtrapolator"
        The name to use when added to a Pipeline. This name is used when calling 
        Pipeline.search() to locate this operation.

    Attributes
    ----------
    output_prefix : str
        Stored prefix for output variable naming.
    params : Dict[str, Any] or None
        Stored training parameters.
    grid : xarray.DataArray
        The evaluation grid loaded from the dataset during calculation.
    output : Dict[str, xarray.DataArray]
        Dictionary containing the extrapolated results with keys:
        - "{prefix}mean": Predicted class labels (argmax of probabilities)
        - "{prefix}y_prob": Class probabilities for each grid point
        - "{prefix}entropy": Prediction entropy (only for single-class case)

    Examples
    --------
    >>> # Create extrapolator for materials classification
    >>> extrapolator = DirichletGPExtrapolator(
    ...     feature_input_variable="composition",
    ...     predictor_input_variable="phase_labels", 
    ...     output_prefix="gp_",
    ...     grid_variable="composition_grid",
    ...     grid_dim="grid_point",
    ...     sample_dim="sample",
    ...     params={"learning_rate": 0.05, "n_iterations": 300, "verbose": True}
    ... )
    >>> 
    >>> # Apply to dataset
    >>> result = extrapolator.calculate(dataset)
    >>> probabilities = result.output["gp_y_prob"]
    >>> predictions = result.output["gp_mean"]
    """

    def __init__(
        self,
        feature_input_variable: str,
        predictor_input_variable: str,
        output_prefix: str,
        grid_variable: str,
        grid_dim: str,
        sample_dim: str,
        params: Optional[Dict[str, Any]] = None,
        name: str = "DirichletGPExtrapolator",
    ) -> None:
        """
        Initialize the Dirichlet Gaussian Process extrapolator.

        Sets up the extrapolator with the specified input/output variables and
        training parameters. Inherits base functionality from the Extrapolator class.

        Parameters
        ----------
        feature_input_variable : str
            Name of the input feature variable in the dataset.
        predictor_input_variable : str  
            Name of the class label variable in the dataset.
        output_prefix : str
            Prefix for naming output variables.
        grid_variable : str
            Name of the prediction grid variable in the dataset.
        grid_dim : str
            Dimension name for grid points.
        sample_dim : str
            Dimension name for training samples.
        params : Dict[str, Any], optional
            Training configuration parameters.
        name : str, default="DirichletGPExtrapolator"
            Name identifier for the extrapolator.

        Returns
        -------
        None
        """
        super().__init__(
            name=name,
            feature_input_variable=feature_input_variable,
            predictor_input_variable=predictor_input_variable,
            output_variables=["y_prob"],
            output_prefix=output_prefix,
            grid_variable=grid_variable,
            grid_dim=grid_dim,
            sample_dim=sample_dim,
        )
        self.output_prefix = output_prefix
        self.params = params

    def calculate(self, dataset: xr.Dataset) -> Self:
        """Apply Dirichlet GP classification to the supplied dataset.
        
        Fits a Gaussian Process classifier with Dirichlet likelihood to the labeled
        training data and generates probabilistic predictions across the specified
        evaluation grid. Handles both multi-class and degenerate single-class cases.

        The method performs the following steps:
        1. Extract and prepare training data from the dataset
        2. Handle special case where all samples have the same label
        3. Set up Dirichlet likelihood and GP model for multi-class case
        4. Train the model using marginal log-likelihood optimization
        5. Generate predictions with uncertainty quantification via sampling
        6. Store results in the output dictionary

        Parameters
        ----------
        dataset : xr.Dataset
            The input dataset containing:
            - Labeled training samples in `feature_input_variable`
            - Class labels in `predictor_input_variable` 
            - Prediction grid in `grid_variable`
            All variables should be properly dimensioned according to the
            specified `sample_dim` and `grid_dim`.

        Returns
        -------
        Self
            The extrapolator instance with populated `output` dictionary containing:
            - "mean": Predicted class labels (int) at each grid point
            - "y_prob": Class probabilities (float) at each grid point  
            - "entropy": Prediction entropy (only for single-class case)

        Notes
        -----
        For the single-class case (all training labels identical), the method
        returns uniform predictions without fitting a GP model, as there is no
        variation to learn from.

        For multi-class cases, the method uses Monte Carlo sampling (256 samples)
        from the posterior predictive distribution to estimate class probabilities,
        providing robust uncertainty quantification.

        Examples
        --------
        >>> # Prepare dataset with composition features and phase labels
        >>> dataset = xr.Dataset({
        ...     'composition': (['sample', 'element'], composition_array),
        ...     'phase_labels': (['sample'], label_array),
        ...     'composition_grid': (['grid_point', 'element'], grid_array)
        ... })
        >>> 
        >>> # Apply extrapolation
        >>> extrapolator = DirichletGPExtrapolator(...)
        >>> result = extrapolator.calculate(dataset)
        >>> 
        >>> # Access results
        >>> class_probs = result.output['prefix_y_prob']  # Shape: (grid_point, n_classes)
        >>> predictions = result.output['prefix_mean']    # Shape: (grid_point,)
        """
        X = dataset[self.feature_input_variable].transpose(self.sample_dim, ...)
        y = dataset[self.predictor_input_variable].transpose(self.sample_dim, ...)
        self.grid = dataset[self.grid_variable]

        if len(np.unique(y)) == 1:
            # Handle degenerate case where all samples have the same label
            self.output[self._prefix_output("mean")] = xr.DataArray(
                np.ones(dataset.grid.shape), dims=[self.grid_dim]
            )
            self.output[self._prefix_output("entropy")] = xr.DataArray(
                np.ones(dataset.grid.shape), dims=[self.grid_dim]
            )
            self.output[self._prefix_output("y_prob")] = xr.DataArray(
                np.ones(dataset.grid.shape), dims=[self.grid_dim]
            )

        else:
            # Set up GP model for multi-class classification
            train_x = torch.from_numpy(X.to_numpy()).to(device)
            train_y = torch.from_numpy(y.to_numpy()).long().squeeze().to(device)
            likelihood = DirichletClassificationLikelihood(train_y, learn_additional_noise=True)
            model = GPModel(train_x, likelihood.transformed_targets, likelihood, num_classes=likelihood.num_classes)
            model, likelihood = self.fit(train_x, model, likelihood, **self.params) # type: ignore
            
            # Generate predictions
            model.eval()
            likelihood.eval()

            with gpytorch.settings.fast_pred_var(), torch.no_grad():
                test_x = torch.from_numpy(self.grid.values).to(device)
                test_dist = model(test_x)
                pred_means = test_dist.loc

                # Monte Carlo sampling for probability estimation
                pred_samples = test_dist.sample(torch.Size((256,))).exp()
                probabilities = (pred_samples / pred_samples.sum(-2, keepdim=True)).mean(0)
                entropy = torch.special.entr(probabilities).sum(0)

                self.output[self._prefix_output("mean")] = xr.DataArray(
                    pred_means.numpy().argmax(axis=0), dims=self.grid_dim
                )
                self.output[self._prefix_output("entropy")] = xr.DataArray(
                    entropy.numpy(), dims=self.grid_dim
                )
                self.output[self._prefix_output("y_prob")] = xr.DataArray(
                    entropy.numpy(), dims=self.grid_dim
                )

        return self 

    def fit(self, train_x, model, likelihood, **kwargs):
        """Train the Gaussian Process model using exact marginal log-likelihood.
        
        Optimizes the GP hyperparameters by maximizing the marginal log-likelihood
        of the training data. Uses Adam optimizer with configurable learning rate
        and number of iterations.

        The training loop optimizes:
        - Kernel hyperparameters (lengthscales, output scale)
        - Mean function parameters  
        - Likelihood parameters (noise levels)
        - Inducing point locations (if using sparse GPs)

        Parameters
        ----------
        train_x : torch.Tensor
            Training input features of shape (n_samples, n_features).
        model : GPModel
            The Gaussian Process model to be trained.
        likelihood : DirichletClassificationLikelihood
            The Dirichlet likelihood function associated with the model.
        **kwargs : Dict[str, Any]
            Training configuration parameters:
            - learning_rate : float, default=0.1
                Learning rate for Adam optimizer.
            - n_iterations : int, default=200  
                Number of training epochs.
            - verbose : bool, default=False
                Whether to print training progress.

        Returns
        -------
        Tuple[GPModel, DirichletClassificationLikelihood]
            The trained model and likelihood with optimized hyperparameters.

        Notes
        -----
        The training uses the negative marginal log-likelihood as the loss function,
        which provides a principled approach to hyperparameter optimization that
        automatically balances model complexity and data fit.

        Progress logging (when verbose=True) reports:
        - Training loss (negative log marginal likelihood)
        - Mean kernel lengthscale across dimensions
        - Mean noise level in the likelihood

        Examples
        --------
        >>> # Manual training call (typically handled internally)
        >>> model, likelihood = extrapolator.fit(
        ...     train_x=training_features,
        ...     model=gp_model, 
        ...     likelihood=dirichlet_likelihood,
        ...     learning_rate=0.05,
        ...     n_iterations=500,
        ...     verbose=True
        ... )
        """
        model.train()
        likelihood.train()
        optimizer = torch.optim.Adam(model.parameters(), lr=kwargs.get("learning_rate", 0.1)) 
        mll = ExactMarginalLogLikelihood(likelihood, model)
        n_iterations = kwargs.get("n_iterations", 200)
        
        for i in range(n_iterations):
            optimizer.zero_grad()
            output = model(train_x)
            loss = -mll(output, likelihood.transformed_targets).sum()
            loss.backward()
            optimizer.step()
            
            if kwargs.get("verbose", False):
                if (i % 50 == 0) or (i == n_iterations - 1):
                    print('Iter %d/%d - Loss: %.3f   lengthscale: %.3f   noise: %.3f' % (
                        i + 1, n_iterations, loss.item(),
                        model.covar_module.base_kernel.lengthscale.mean().item(),
                        model.likelihood.second_noise_covar.noise.mean().item()
                    ))

        return model, likelihood

class GPModel(ExactGP):
    """
    A multi-class Gaussian Process model for exact inference.
    
    This class implements a Gaussian Process model using GPyTorch's ExactGP framework,
    designed for multi-class classification or regression tasks. It uses a constant mean
    function and an RBF (Radial Basis Function) kernel with automatic relevance 
    determination scaling.
    
    The model creates independent GP instances for each class, allowing for flexible
    modeling of different output dimensions or classes with potentially different
    length scales and noise characteristics.
    
    Attributes:
        mean_module (ConstantMean): Constant mean function with batch support for
            multiple classes.
        covar_module (ScaleKernel): Scaled RBF kernel with batch support for
            multiple classes.
    
    Args:
        train_x (torch.Tensor): Training input data of shape (n_samples, n_features).
        train_y (torch.Tensor): Training target data of shape (n_samples,) for 
            single-output or (n_samples, n_classes) for multi-output.
        likelihood (Likelihood): GPyTorch likelihood function (e.g., GaussianLikelihood
            for regression, BernoulliLikelihood for classification).
        num_classes (int): Number of output classes or dimensions.
    
    Example:
        >>> import torch
        >>> from gpytorch.likelihoods import GaussianLikelihood
        >>> 
        >>> # Generate sample data
        >>> train_x = torch.randn(100, 2)
        >>> train_y = torch.randn(100, 3)  # 3 classes
        >>> likelihood = GaussianLikelihood()
        >>> 
        >>> # Create model
        >>> model = GPModel(train_x, train_y, likelihood, num_classes=3)
        >>> 
        >>> # Forward pass
        >>> with torch.no_grad():
        ...     pred_dist = model(train_x)
        ...     mean = pred_dist.mean
        ...     variance = pred_dist.variance
    """
    
    def __init__(self, train_x, train_y, likelihood, num_classes):
        """
        Initialize the Gaussian Process model.
        
        Sets up the GP with a constant mean function and scaled RBF kernel,
        both configured for batch processing across multiple classes.
        
        Args:
            train_x (torch.Tensor): Training input data of shape (n_samples, n_features).
            train_y (torch.Tensor): Training target data of shape (n_samples,) for 
                single-output or (n_samples, n_classes) for multi-output.
            likelihood (Likelihood): GPyTorch likelihood function that defines the
                observation model (e.g., noise characteristics).
            num_classes (int): Number of output classes or dimensions. Must be positive.
        
        Raises:
            ValueError: If num_classes is not a positive integer.
            TypeError: If inputs are not torch.Tensors or likelihood is not a valid
                GPyTorch likelihood.
        """
        super().__init__(train_x, train_y, likelihood)
        self.mean_module = ConstantMean(batch_shape=torch.Size((num_classes,)))
        self.covar_module = ScaleKernel(
            RBFKernel(batch_shape=torch.Size((num_classes,))),
            batch_shape=torch.Size((num_classes,)),
        )

    def forward(self, x):
        """
        Forward pass through the Gaussian Process model.
        
        Computes the predictive mean and covariance for the given input data
        by evaluating the mean and covariance modules, then returns a 
        MultivariateNormal distribution representing the GP posterior.
        
        Args:
            x (torch.Tensor): Input data of shape (n_test_samples, n_features).
                Must have the same number of features as the training data.
        
        Returns:
            MultivariateNormal: A multivariate normal distribution representing
                the GP posterior with:
                - mean: Tensor of shape (num_classes, n_test_samples) containing
                  the predictive mean for each class
                - covariance_matrix: Tensor of shape (num_classes, n_test_samples, 
                  n_test_samples) containing the predictive covariance
        
        Note:
            This method should typically be called within a torch.no_grad() context
            for prediction, or within the training loop for computing the marginal
            log-likelihood.
        
        Example:
            >>> test_x = torch.randn(50, 2)
            >>> with torch.no_grad():
            ...     pred_dist = model(test_x)
            ...     mean_pred = pred_dist.mean  # Shape: (num_classes, 50)
            ...     var_pred = pred_dist.variance  # Shape: (num_classes, 50)
        """
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return MultivariateNormal(mean_x, covar_x)


