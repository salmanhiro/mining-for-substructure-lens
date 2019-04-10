import sys, os

sys.path.append("../")

import logging

DEBUG = False
AUTOGRAD = False

if AUTOGRAD:
    import autograd.numpy as np
else:
    import numpy as np
import autograd as ag

from simulation.units import (
    M_s,
    erg,
    Centimeter,
    Angstrom,
    Sec,
    radtoasc,
)  # Don't import * since that will overwrite np
from simulation.lensing_sim import LensingSim

logger = logging.getLogger(__name__)


class SubhaloSimulator:
    def __init__(
        self,
        resolution=64,
        coordinate_limit=2.5,
        mass_base_unit=1.0e9 * M_s,
        m_sub_min=0.01,
        m_sub_high=1.0,
        host_profile="sis",
        host_theta_x=0.01,
        host_theta_y=-0.01,
        host_theta_E=1.0,
        exposure=(1 / 1.8e-19) * erg ** -1 * Centimeter ** 2 * Angstrom * 1000 * Sec,
        A_iso=2e-7 * erg / Centimeter ** 2 / Sec / Angstrom / (radtoasc) ** 2,
        zs=1.0,
        zl=0.1,
        src_profile="sersic",
        src_I_gal=1e-17 * erg / Centimeter ** 2 / Sec / Angstrom,
        src_theta_e_gal=0.5,
        src_n=4,
        epsilon=1.0e-4,
    ):
        self.mass_base_unit = mass_base_unit
        self.resolution = resolution
        self.coordinate_limit = coordinate_limit
        self.m_sub_min = m_sub_min
        self.m_sub_high = m_sub_high

        # Host galaxy
        self.hst_param_dict = {
            "profile": host_profile,
            "theta_x": host_theta_x,
            "theta_y": host_theta_y,
            "theta_E": host_theta_E,
        }

        # Observational parameters
        self.observation_dict = {
            "nx": resolution,
            "ny": resolution,
            "xlims": (-coordinate_limit, coordinate_limit),
            "ylims": (-coordinate_limit, coordinate_limit),
            "exposure": exposure,
            "A_iso": A_iso,
        }

        # Global parameters
        self.global_dict = {"z_s": zs, "z_l": zl}

        # Source parameters
        self.src_param_dict = {
            "profile": src_profile,
            "I_gal": src_I_gal,
            "theta_e_gal": src_theta_e_gal,
            "n_srsc": src_n,
        }

        # Autograd
        if AUTOGRAD:
            self.d_simulate = ag.grad_and_aux(self.simulate)
        else:
            self.d_simulate = self.simulate

        self.epsilon = epsilon

    def simulate(self, params, params_eval):
        """
        Generates one observed lensed image for given parameters of the subhalo mass distribution
        dn/dm = alpha (m/M_s)^beta with m > m_min and parameters alpha > 0, beta < -1.

        Subhalo coordinates (x,y) are sampled uniformly.
        """

        # Prepare parameters and joint likelihood
        alpha = params[0]
        beta = params[1]
        alphas_eval = [alpha] + [param[0] for param in params_eval]
        betas_eval = [beta] + [param[1] for param in params_eval]
        log_p_xz_eval = [0.0 for _ in alphas_eval]

        # Number of subhalos
        n_sub = self._draw_n_sub(alpha, beta)

        # Evaluate likelihoods of numbers of subhalos
        for i_eval, (alpha_eval, beta_eval) in enumerate(zip(alphas_eval, betas_eval)):
            log_p_xz_eval[i_eval] += self._calculate_log_p_n_sub(
                n_sub, alpha_eval, beta_eval
            )
        if DEBUG:
            logger.debug("Log p: %s", log_p_xz_eval)

        # Draw subhalo masses
        m_sub = self._draw_m_sub(n_sub, alpha, beta)
        if DEBUG:
            logger.debug("Subhalo masses: %s", m_sub)

        # Evaluate likelihoods of subhalo masses
        for i_eval, (alpha_eval, beta_eval) in enumerate(zip(alphas_eval, betas_eval)):
            for i_sub in range(n_sub):
                log_p_xz_eval[i_eval] += self._calculate_log_p_m_sub(
                    m_sub[i_sub], alpha_eval, beta_eval
                )
        if DEBUG:
            logger.debug("Log p: %s", log_p_xz_eval)

        m_sub = self._detach(m_sub)

        # Subhalo coordinates
        x_sub, y_sub = self._draw_sub_coordinates(n_sub)
        if DEBUG:
            logger.debug("Subhalo x: %s", x_sub)
        if DEBUG:
            logger.debug("Subhalo y: %s", y_sub)

        # Lensing simulation
        image_mean = self._lensing(n_sub, m_sub, x_sub, y_sub)
        if DEBUG:
            logger.debug("Image mean: %s", image_mean)

        # dx/dz

        # Observed lensed image
        image = self._observation(image_mean)
        if DEBUG:
            logger.debug("Image: %s", image)

        # Returns
        latent_variables = (n_sub, m_sub, x_sub, y_sub, image_mean, image)
        return log_p_xz_eval[0], (image, log_p_xz_eval, latent_variables)

    def _calculate_expected_n_sub(self, alpha, beta):
        return alpha * (self.m_sub_min / self.m_sub_high) ** (1.0 + beta)

    def _draw_n_sub(self, alpha, beta):
        n_sub_mean = self._calculate_expected_n_sub(alpha, beta)
        n_sub_mean = self._detach(n_sub_mean)
        if DEBUG:
            logger.debug("Poisson mean: %s", n_sub_mean)

        # Draw number of subhalos
        n_sub = np.random.poisson(n_sub_mean)
        if DEBUG:
            logger.debug("Number of subhalos: %s", n_sub)

        return n_sub

    def _calculate_log_p_n_sub(self, n_sub, alpha, beta):
        n_sub_mean_eval = self._calculate_expected_n_sub(alpha, beta)
        if DEBUG:
            logger.debug("Eval subhalo mean: %s", n_sub_mean_eval)
        log_p_poisson = (
            n_sub * np.log(n_sub_mean_eval) - n_sub_mean_eval
        )  #  - np.log(math.factorial(n_sub))
        return log_p_poisson

    def _draw_m_sub(self, n_sub, alpha, beta):
        u = np.random.uniform(0, 1, size=n_sub)
        m_sub = self.m_sub_min * (1 - u) ** (1.0 / (beta + 1.0))
        return m_sub

    def _calculate_log_p_m_sub(self, m, alpha, beta):
        log_p = (
            np.log(-beta - 1.0)
            - np.log(self.m_sub_min)
            + beta * np.log(m / self.m_sub_min)
        )
        return log_p

    def _draw_sub_coordinates(self, n_sub):
        phi_sub = np.random.uniform(low=0.0, high=2.0 * np.pi, size=n_sub)
        r_sub = np.random.uniform(low=0.9, high=1.5, size=n_sub)
        x_sub = r_sub * np.cos(phi_sub)
        y_sub = r_sub * np.sin(phi_sub)
        # x_sub = np.random.uniform(
        #    low=-self.coordinate_limit, high=self.coordinate_limit, size=n_sub
        # )
        # y_sub = np.random.uniform(
        #    low=-self.coordinate_limit, high=self.coordinate_limit, size=n_sub
        # )
        return x_sub, y_sub

    def _lensing(self, n_sub, m_sub, x_sub, y_sub):
        lens_list = [self.hst_param_dict]
        for i_sub in range(n_sub):
            sub_param_dict = {
                "profile": "nfw",
                "theta_x": x_sub[i_sub],
                "theta_y": y_sub[i_sub],
                "M200": m_sub[i_sub] * self.mass_base_unit,
            }
            lens_list.append(sub_param_dict)
        lsi = LensingSim(
            lens_list, [self.src_param_dict], self.global_dict, self.observation_dict
        )
        image_mean = lsi.lensed_image()
        return image_mean

    def _observation(self, image_mean):
        return np.random.poisson(image_mean)

    @staticmethod
    def _detach(obj):
        try:
            obj = obj._value
        except AttributeError:
            pass
        return obj

    def rvs(self, alpha, beta, n_images):
        all_images = []
        all_latents = []

        n_verbose = max(1, n_images // 100)

        for i_sim in range(n_images):
            if (i_sim + 1) % n_verbose == 0:
                logger.info("Simulating image %s / %s", i_sim + 1, n_images)
            else:
                logger.debug("Simulating image %s / %s", i_sim + 1, n_images)

            params = self._wrap_params(alpha, beta, i_sim, n_images)
            _, (image, _, latents) = self.simulate(params, [])

            n_subhalos = latents[0]
            logger.debug("Image generated with %s subhalos", n_subhalos)

            all_images.append(image)
            all_latents.append(latents)

        all_images = np.array(all_images)

        return all_images, all_latents

    def rvs_score_ratio(self, alpha, beta, alpha_ref, beta_ref, n_images):
        all_images = []
        all_t_xz = []
        all_log_r_xz = []
        all_latents = []

        n_verbose = max(1, n_images // 100)

        for i_sim in range(n_images):
            if (i_sim + 1) % n_verbose == 0:
                logger.info("Simulating image %s / %s", i_sim + 1, n_images)
            else:
                logger.debug("Simulating image %s / %s", i_sim + 1, n_images)

            params = self._wrap_params(alpha, beta, i_sim, n_images)
            params_ref = self._wrap_params(alpha_ref, beta_ref, i_sim, n_images)

            params_eval = [params_ref]
            if not AUTOGRAD:
                params_eval = self._add_epsilon_shifts(params, params_eval)

            t_xz, (image, log_p_xzs, latents) = self.d_simulate(params, params_eval)
            log_r_xz = log_p_xzs[0] - log_p_xzs[1]
            if not AUTOGRAD:
                t_xz = self._finite_diff(log_p_xzs)

            # Clean up
            t_xz = self._detach(t_xz)
            log_r_xz = self._detach(log_r_xz)

            n_subhalos = latents[0]
            logger.debug("Image generated with %s subhalos", n_subhalos)

            logger.debug("Joint log r: %s", log_r_xz)
            logger.debug("Joint score: %s", t_xz)

            all_images.append(image)
            all_t_xz.append(t_xz)
            all_log_r_xz.append(log_r_xz)
            all_latents.append(latents)

        all_images = np.array(all_images)
        all_t_xz = np.array(all_t_xz)
        all_log_r_xz = np.array(all_log_r_xz)

        return all_images, all_t_xz, all_log_r_xz, all_latents

    def rvs_score_ratio_to_evidence(
        self,
        alpha,
        beta,
        alpha_mean,
        alpha_std,
        beta_mean,
        beta_std,
        n_images,
        n_theta_samples,
    ):
        all_images = []
        all_t_xz = []
        all_log_r_xz = []
        all_log_r_xz_uncertainties = []
        all_latents = []

        n_verbose = max(1, n_images // 100)

        alpha_prior = np.random.normal(
            loc=alpha_mean, scale=alpha_std, size=n_theta_samples
        )
        beta_prior = np.random.normal(
            loc=beta_mean, scale=beta_std, size=n_theta_samples
        )
        alpha_prior = np.clip(alpha_prior, 0.1, None)
        beta_prior = np.clip(beta_prior, None, -1.1)
        params_prior = np.vstack((alpha_prior, beta_prior)).T

        for i_sim in range(n_images):
            if (i_sim + 1) % n_verbose == 0:
                logger.info("Simulating image %s / %s", i_sim + 1, n_images)
            else:
                logger.debug("Simulating image %s / %s", i_sim + 1, n_images)

            params = self._wrap_params(alpha, beta, i_sim, n_images)

            params_eval = params_prior
            if not AUTOGRAD:
                params_eval = self._add_epsilon_shifts(params, params_eval)

            t_xz, (image, log_p_xzs, latents) = self.d_simulate(params, params_eval)

            # Clean up
            t_xz = self._detach(t_xz)
            for i, log_p_xz in enumerate(log_p_xzs):
                log_p_xzs[i] = self._detach(log_p_xz)

            # Evaluate likelihood ratio wrt evidence
            inverse_r_xz = 0.0
            for i_theta in range(n_theta_samples):
                inverse_r_xz += np.exp(log_p_xzs[i_theta + 1] - log_p_xzs[0])
            inverse_r_xz /= float(n_theta_samples)
            log_r_xz = -np.log(inverse_r_xz)

            # Estimate uncertainty of log r from MC sampling
            inverse_r_xz_uncertainty = 0.0
            for i_theta in range(n_theta_samples):
                inverse_r_xz_uncertainty += (
                    np.exp(log_p_xzs[i_theta + 1] - log_p_xzs[0]) - inverse_r_xz
                ) ** 2.0
            inverse_r_xz_uncertainty /= float(n_theta_samples) * (
                float(n_theta_samples) - 1.0
            )
            log_r_xz_uncertainty = inverse_r_xz_uncertainty / inverse_r_xz

            n_subhalos = latents[0]
            logger.debug("Image generated with %s subhalos", n_subhalos)

            # Calculate score from finite diffs
            if not AUTOGRAD:
                t_xz = self._finite_diff(log_p_xzs)
            logger.debug("Joint log r: %s", log_r_xz)
            logger.debug("Joint score: %s", t_xz)

            all_images.append(image)
            all_t_xz.append(t_xz)
            all_log_r_xz.append(log_r_xz)
            all_log_r_xz_uncertainties.append(log_r_xz_uncertainty)
            all_latents.append(latents)

        all_images = np.array(all_images)
        all_t_xz = np.array(all_t_xz)
        all_log_r_xz = np.array(all_log_r_xz)
        all_log_r_xz_uncertainties = np.array(all_log_r_xz_uncertainties)

        return (
            all_images,
            all_t_xz,
            all_log_r_xz,
            all_log_r_xz_uncertainties,
            all_latents,
        )

    def rvs_score_ratio_to_evidence_inverse(
        self,
        alpha,
        beta,
        alpha_mean,
        alpha_std,
        beta_mean,
        beta_std,
        n_images,
        n_theta_samples,
    ):
        all_images = []
        all_t_xz = []
        all_log_r_xz = []
        all_log_r_xz_uncertainties = []
        all_latents = []

        n_verbose = max(1, n_images // 100)

        alpha_prior = np.random.normal(
            loc=alpha_mean, scale=alpha_std, size=n_theta_samples
        )
        beta_prior = np.random.normal(
            loc=beta_mean, scale=beta_std, size=n_theta_samples
        )
        alpha_prior = np.clip(alpha_prior, 0.1, None)
        beta_prior = np.clip(beta_prior, None, -1.1)
        params_prior = np.vstack((alpha_prior, beta_prior)).T

        for i_sim in range(n_images):
            if (i_sim + 1) % n_verbose == 0:
                logger.info("Simulating image %s / %s", i_sim + 1, n_images)
            else:
                logger.debug("Simulating image %s / %s", i_sim + 1, n_images)

            # Choose one theta from prior that we use for sampling here
            i_sample = np.random.randint(n_theta_samples)
            params_sample = params_prior[i_sample]

            # Numerator hypothesis
            params = self._wrap_params(alpha, beta, i_sim, n_images)
            params_eval = np.vstack((params, params_prior))
            if not AUTOGRAD:
                params_eval = self._add_epsilon_shifts(params, params_eval)

            t_xz, (image, log_p_xzs, latents) = self.d_simulate(
                params_sample, params_eval
            )

            # Clean up
            t_xz = self._detach(t_xz)
            for i, log_p_xz in enumerate(log_p_xzs):
                log_p_xzs[i] = self._detach(log_p_xz)

            # Evaluate likelihood ratio wrt evidence
            inverse_r_xz = 0.0
            for i_theta in range(n_theta_samples):
                inverse_r_xz += np.exp(log_p_xzs[i_theta + 2] - log_p_xzs[1])
            inverse_r_xz /= float(n_theta_samples)
            log_r_xz = -np.log(inverse_r_xz)

            # Estimate uncertainty of log r from MC sampling
            inverse_r_xz_uncertainty = 0.0
            for i_theta in range(n_theta_samples):
                inverse_r_xz_uncertainty += (
                    np.exp(log_p_xzs[i_theta + 1] - log_p_xzs[0]) - inverse_r_xz
                ) ** 2.0
            inverse_r_xz_uncertainty /= float(n_theta_samples) * (
                float(n_theta_samples) - 1.0
            )
            log_r_xz_uncertainty = inverse_r_xz_uncertainty / inverse_r_xz

            n_subhalos = latents[0]
            logger.debug("Image generated with %s subhalos", n_subhalos)

            # Calculate score from finite diffs
            if not AUTOGRAD:
                t_xz = self._finite_diff(log_p_xzs)

            logger.debug("Joint log r: %s", log_r_xz)
            logger.debug("Joint score: %s", t_xz)

            all_images.append(image)
            all_t_xz.append(t_xz)
            all_log_r_xz.append(log_r_xz)
            all_log_r_xz_uncertainties.append(log_r_xz_uncertainty)
            all_latents.append(latents)

        all_images = np.array(all_images)
        all_t_xz = np.array(all_t_xz)
        all_log_r_xz = np.array(all_log_r_xz)
        all_log_r_xz_uncertainties = np.array(all_log_r_xz_uncertainties)

        return (
            all_images,
            all_t_xz,
            all_log_r_xz,
            all_log_r_xz_uncertainties,
            all_latents,
        )

    @staticmethod
    def _wrap_params(alphas, betas, i, n):
        try:
            assert len(alphas) == n
            this_alpha = alphas[i]
        except TypeError:
            this_alpha = alphas
        try:
            assert len(betas) == n
            this_beta = betas[i]
        except TypeError:
            this_beta = betas
        params = np.array([this_alpha, this_beta])
        return params

    def _add_epsilon_shifts(self, params, add_to=None):
        eps_vec0 = np.asarray(params).flatten() + np.array([self.epsilon, 0.0]).reshape(
            1, 2
        )
        eps_vec1 = np.asarray(params).flatten() + np.array([0.0, self.epsilon]).reshape(
            1, 2
        )
        params = params.reshape(1, 2)

        if add_to is None:
            new_params = np.vstack([params, eps_vec0, eps_vec1])
        else:
            new_params = np.vstack([add_to, params, eps_vec0, eps_vec1])

        return new_params

    def _finite_diff(self, log_ps):
        t0 = (log_ps[-2] - log_ps[-3]) / self.epsilon
        t1 = (log_ps[-1] - log_ps[-3]) / self.epsilon
        return np.array([t0, t1])