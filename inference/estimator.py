from __future__ import absolute_import, division, print_function

import logging
import os
import json
import numpy as np
from collections import OrderedDict
import torch

from inference.models.vgg import VGGRatioEstimator
from inference.models.resnet import ResNetRatioEstimator
from inference.trainer import SingleParameterizedRatioTrainer
from inference.utils import (
    create_missing_folders,
    load_and_check,
    get_optimizer,
    get_loss,
)
from inference.utils import restrict_samplesize

logger = logging.getLogger(__name__)


class ParameterizedRatioEstimator(object):
    theta_mean = np.array([10.0, -1.9])
    theta_std = np.array([3.0, 0.3])

    def __init__(
        self,
        resolution=64,
        n_parameters=2,
        architecture="resnet",
        log_input=False,
        rescale_inputs=True,
        rescale_theta=True,
    ):
        self.resolution = resolution
        self.n_parameters = n_parameters
        self.log_input = log_input
        self.rescale_inputs = rescale_inputs
        self.rescale_theta = rescale_theta
        self.architecture = architecture

        self.x_scaling_mean = None
        self.x_scaling_std = None

        self._create_model()

    def train(
        self,
        method,
        x,
        y,
        theta,
        r_xz=None,
        t_xz=None,
        alpha=1.0,
        optimizer="adam",
        n_epochs=50,
        batch_size=256,
        initial_lr=0.001,
        final_lr=0.0001,
        nesterov_momentum=None,
        validation_split=0.25,
        early_stopping=True,
        limit_samplesize=None,
        verbose="some",
        update_input_rescaling=True,
    ):

        logger.info("Starting training")
        logger.info("  Method:                 %s", method)
        if method in ["cascal", "rascal", "alices"]:
            logger.info("  alpha:                  %s", alpha)
        logger.info("  Batch size:             %s", batch_size)
        logger.info("  Optimizer:              %s", optimizer)
        logger.info("  Epochs:                 %s", n_epochs)
        logger.info(
            "  Learning rate:          %s initially, decaying to %s",
            initial_lr,
            final_lr,
        )
        if optimizer == "sgd":
            logger.info("  Nesterov momentum:      %s", nesterov_momentum)
        logger.info("  Validation split:       %s", validation_split)
        logger.info("  Early stopping:         %s", early_stopping)
        if limit_samplesize is None:
            logger.info("  Samples:                all")
        else:
            logger.info("  Samples:                %s", limit_samplesize)
        logger.info("  Update x rescaling:     %s", update_input_rescaling)

        # Load training data
        logger.info("Loading training data")
        theta = load_and_check(theta)
        x = load_and_check(x)
        y = load_and_check(y)
        r_xz = load_and_check(r_xz)
        t_xz = load_and_check(t_xz)

        self._check_required_data(method, r_xz, t_xz)
        if update_input_rescaling:
            self._initialize_input_transform(x)

        # Clean up input data
        y = y.reshape((-1, 1))
        if r_xz is not None:
            r_xz = r_xz.reshape((-1, 1))

        # Rescale theta and t_xz
        theta = self._transform_theta(theta)
        if t_xz is not None:
            t_xz = self._transform_t_xz(t_xz)

        # Infer dimensions of problem
        n_samples = x.shape[0]
        n_parameters = theta.shape[1]
        resolution_x = x.shape[1]
        resolution_y = x.shape[2]
        logger.info(
            "Found %s samples with %s parameters and resolution %s x %s",
            n_samples,
            n_parameters,
            resolution_x,
            resolution_y,
        )
        if resolution_x != resolution_y:
            raise RuntimeError(
                "Currently only supports square images, but found resolution {} x {}".format(
                    resolution_x, resolution_y
                )
            )
        resolution = resolution_x

        # Limit sample size
        if limit_samplesize is not None and limit_samplesize < n_samples:
            logger.info(
                "Only using %s of %s training samples", limit_samplesize, n_samples
            )
            x, theta, y, r_xz, t_xz = restrict_samplesize(
                limit_samplesize, x, theta, y, r_xz, t_xz
            )

        # Check consistency of input with model
        if n_parameters != self.n_parameters:
            raise RuntimeError(
                "Number of parameters does not match model: {} vs {}".format(
                    n_parameters, self.n_parameters
                )
            )
        if resolution != self.resolution:
            raise RuntimeError(
                "Number of observables does not match model: {} vs {}".format(
                    resolution, self.resolution
                )
            )

        # Data
        data = self._package_training_data(method, x, theta, y, r_xz, t_xz)

        # Losses
        loss_functions, loss_labels, loss_weights = get_loss(method, alpha)

        # Optimizer
        opt, opt_kwargs = get_optimizer(optimizer, nesterov_momentum)

        # Train model
        logger.info("Training model")
        trainer = SingleParameterizedRatioTrainer(self.model)
        result = trainer.train(
            data=data,
            loss_functions=loss_functions,
            loss_weights=loss_weights,
            loss_labels=loss_labels,
            epochs=n_epochs,
            batch_size=batch_size,
            optimizer=opt,
            optimizer_kwargs=opt_kwargs,
            initial_lr=initial_lr,
            final_lr=final_lr,
            validation_split=validation_split,
            early_stopping=early_stopping,
            verbose=verbose,
        )
        return result

    def log_likelihood_ratio(
        self,
        x,
        theta,
        test_all_combinations=True,
        evaluate_score=False,
        evaluate_grad_x=False,
        batch_size=1024,
        grad_x_theta_index=0
    ):
        if self.model is None:
            raise ValueError("No model -- train or load model before evaluating it!")

        # Load training data
        logger.debug("Loading evaluation data")
        x = load_and_check(x)
        theta = load_and_check(theta)

        # Rescale theta
        theta = self._transform_theta(theta)

        # Evaluate
        if test_all_combinations:
            logger.debug("Starting ratio evaluation for all combinations")

            all_log_r_hat = []
            all_t_hat = []
            all_grad_x = None

            for i, this_theta in enumerate(theta):
                logger.debug(
                    "Starting ratio evaluation for thetas %s / %s: %s",
                    i + 1,
                    len(theta),
                    this_theta,
                )
                _, log_r_hat, t_hat, x_grad = self._evaluate(
                    theta0s=[this_theta],
                    xs=x,
                    evaluate_score=evaluate_score,
                    evaluate_grad_x=evaluate_grad_x,
                    batch_size=batch_size,
                )

                all_log_r_hat.append(log_r_hat)
                all_t_hat.append(t_hat)
                if x_grad is not None and i == grad_x_theta_index:
                    all_grad_x = x_grad

            all_log_r_hat = np.array(all_log_r_hat)
            all_t_hat = np.array(all_t_hat)

        else:
            logger.debug("Starting ratio evaluation")
            _, all_log_r_hat, all_t_hat, all_grad_x = self._evaluate(
                theta0s=theta,
                xs=x,
                evaluate_score=evaluate_score,
                evaluate_grad_x=evaluate_grad_x,
                batch_size=batch_size,
            )

        logger.debug("Evaluation done")
        return all_log_r_hat, all_t_hat, all_grad_x

    def _evaluate(
        self,
        theta0s,
        xs,
        evaluate_score=False,
        evaluate_grad_x=False,
        run_on_gpu=True,
        double_precision=False,
        batch_size=1000,
    ):
        # Batches
        n_xs = len(xs)
        n_batches = (n_xs - 1) // batch_size + 1

        # results
        all_s, all_log_r, all_t, all_x_grad = [], [], [], []

        for i_batch in range(n_batches):
            x_batch = np.copy(xs[i_batch * batch_size : (i_batch + 1) * batch_size])
            if len(theta0s) == n_xs:
                theta_batch = np.copy(
                    theta0s[i_batch * batch_size : (i_batch + 1) * batch_size]
                )
            else:
                theta_batch = np.copy(theta0s)

            s, log_r, t, x_grad = self._evaluate_batch(
                theta_batch,
                x_batch,
                evaluate_score,
                evaluate_grad_x,
                run_on_gpu,
                double_precision,
            )

            all_s.append(s)
            all_log_r.append(log_r)
            if t is not None:
                all_t.append(t)
            if all_x_grad is not None:
                all_x_grad.append(x_grad)

        # mash together
        all_s = np.concatenate(all_s, 0)
        all_log_r = np.concatenate(all_log_r, 0)
        if len(all_t) > 0:
            all_t = np.concatenate(all_t, 0)
        else:
            all_t = None
        if len(all_x_grad) > 0:
            all_x_grad = np.concatenate(all_x_grad, 0)
        else:
            all_x_grad = None

        return all_s, all_log_r, all_t, all_x_grad

    def _evaluate_batch(
        self, theta0s, xs, evaluate_score, evaluate_grad_x, run_on_gpu, double_precision
    ):
        # CPU or GPU?
        run_on_gpu = run_on_gpu and torch.cuda.is_available()
        device = torch.device("cuda" if run_on_gpu else "cpu")
        dtype = torch.double if double_precision else torch.float

        # Prepare data
        n_xs = len(xs)
        theta0s = torch.stack(
            [
                torch.tensor(theta0s[i % len(theta0s)], requires_grad=evaluate_score)
                for i in range(n_xs)
            ]
        )
        xs = torch.stack([torch.tensor(x) for x in xs])

        self.model = self.model.to(device, dtype)
        theta0s = theta0s.to(device, dtype)
        xs = xs.to(device, dtype)

        # Evaluate ratio estimator with score or x gradients:
        if evaluate_score or evaluate_grad_x:
            self.model.eval()

            s, log_r, t, x_grad = self.model(
                theta0s,
                xs,
                track_score=evaluate_score,
                return_grad_x=evaluate_grad_x,
                create_gradient_graph=False,
            )

            # Copy back tensors to CPU
            if run_on_gpu:
                s = s.cpu()
                log_r = log_r.cpu()
                if t is not None:
                    t = t.cpu()
                if x_grad is not None:
                    x_grad = x_grad.cpu()

            # Get data and return
            s = s.detach().numpy().flatten()
            log_r = log_r.detach().numpy().flatten()
            if t is not None:
                t = t.detach().numpy()
            if x_grad is not None:
                x_grad = x_grad.detach().numpy()

        # Evaluate ratio estimator without score:
        else:
            with torch.no_grad():
                self.model.eval()

                s, log_r, _, _ = self.model(
                    theta0s,
                    xs,
                    track_score=False,
                    return_grad_x=False,
                    create_gradient_graph=False,
                )

                # Copy back tensors to CPU
                if run_on_gpu:
                    s = s.cpu()
                    log_r = log_r.cpu()

                # Get data and return
                s = s.detach().numpy().flatten()
                log_r = log_r.detach().numpy().flatten()
                t = None
                x_grad = None

        return s, log_r, t, x_grad

    def save(self, filename, save_model=False):
        if self.model is None:
            raise ValueError("No model -- train or load model before saving!")

        # Check paths
        create_missing_folders([os.path.dirname(filename)])

        # Save settings
        logger.debug("Saving settings to %s_settings.json", filename)
        settings = self._wrap_settings()

        with open(filename + "_settings.json", "w") as f:
            json.dump(settings, f)

        # Save state dict
        logger.debug("Saving state dictionary to %s_state_dict.pt", filename)
        torch.save(self.model.state_dict(), filename + "_state_dict.pt")

        # Save model
        if save_model:
            logger.debug("Saving model to %s_model.pt", filename)
            torch.save(self.model, filename + "_model.pt")

    def load(self, filename):
        # Load settings and create model
        logger.debug("Loading settings from %s_settings.json", filename)
        with open(filename + "_settings.json", "r") as f:
            settings = json.load(f)
        self._unwrap_settings(settings)
        self._create_model()

        # Load state dict
        logger.debug("Loading state dictionary from %s_state_dict.pt", filename)
        self.model.load_state_dict(
            torch.load(filename + "_state_dict.pt", map_location="cpu")
        )

    def _create_model(self):
        logger.info("Creating model")
        logger.info("  Architecture:           %s", self.architecture)
        logger.info("  Log input:              %s", self.log_input)
        logger.info(
            "  Rescale input:          %s",
            self.x_scaling_std is not None and self.x_scaling_mean is not None,
        )

        if self.architecture in ["resnet", "resnet18"]:
            self.model = ResNetRatioEstimator(
                n_parameters=self.n_parameters,
                n_hidden=512,
                log_input=self.log_input,
                input_mean=self.x_scaling_mean,
                input_std=self.x_scaling_std,
            )

        elif self.architecture == "resnet50":
            self.model = ResNetRatioEstimator(
                n_parameters=self.n_parameters,
                cfg=50,
                n_hidden=1024,
                log_input=self.log_input,
                input_mean=self.x_scaling_mean,
                input_std=self.x_scaling_std,
            )

        elif self.architecture == "vgg":
            self.model = VGGRatioEstimator(
                n_parameters=self.n_parameters,
                log_input=self.log_input,
                input_mean=self.x_scaling_mean,
                input_std=self.x_scaling_std,
            )

        else:
            raise RuntimeError("Unknown architecture {}".format(self.architecture))

        logger.info("Model has %s trainable parameters", self._count_model_parameters())

    def _count_model_parameters(self):
        return sum(p.numel() for p in self.model.parameters() if p.requires_grad)

    def _initialize_input_transform(self, x):
        if self.rescale_inputs:
            self.x_scaling_mean = np.mean(x)
            self.x_scaling_std = np.maximum(np.std(x), 1.0e-6)
        else:
            self.x_scaling_mean = None
            self.x_scaling_std = None

        self.model.input_mean = self.x_scaling_mean
        self.model.input_std = self.x_scaling_std

    def _transform_theta(self, theta):
        if self.rescale_theta:
            theta = theta - self.theta_mean[np.newaxis, :]
            theta = theta / self.theta_std[np.newaxis, :]
        return theta

    def _transform_t_xz(self, t_xz):
        if self.rescale_theta:
            t_xz = t_xz * self.theta_std[np.newaxis, :]
        return t_xz

    def _wrap_settings(self):
        settings = {
            "resolution": self.resolution,
            "n_parameters": self.n_parameters,
            "architecture": self.architecture,
            "log_input": self.log_input,
            "rescale_inputs": self.rescale_inputs,
            "x_scaling_mean": self.x_scaling_mean,
            "x_scaling_std": self.x_scaling_std,
            "rescale_theta": self.rescale_theta,
        }
        return settings

    def _unwrap_settings(self, settings):
        self.resolution = int(settings["resolution"])
        self.n_parameters = int(settings["n_parameters"])
        self.architecture = str(settings["architecture"])
        self.log_input = bool(settings["log_input"])
        self.rescale_inputs = str(settings["rescale_inputs"])
        self.x_scaling_mean = float(settings["x_scaling_mean"])
        self.x_scaling_std = float(settings["x_scaling_std"])
        self.rescale_theta = bool(settings["rescale_theta"])

    @staticmethod
    def _check_required_data(method, r_xz, t_xz):
        if method in ["cascal", "alices", "rascal"] and t_xz is None:
            raise RuntimeError(
                "Method {} requires joint score information".format(method)
            )
        if method in ["rolr", "alices", "rascal"] and r_xz is None:
            raise RuntimeError(
                "Method {} requires joint likelihood ratio information".format(method)
            )

    @staticmethod
    def _package_training_data(method, x, theta, y, r_xz, t_xz):
        data = OrderedDict()
        data["x"] = x
        data["theta"] = theta
        data["y"] = y
        if method in ["rolr", "alice", "alices", "rascal"]:
            data["r_xz"] = r_xz
        if method in ["cascal", "alices", "rascal"]:
            data["t_xz"] = t_xz
        return data