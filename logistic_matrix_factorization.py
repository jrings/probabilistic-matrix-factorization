import numpy as np
import scipy as scipy
import scipy.linalg
import scipy.sparse
import pypolyagamma
import math
import joblib
import pdb

class MatrixFactorization(object):

    def __init__(self, val, row_id_var, col_id_var, num_factor, bias_scale, factor_scale, weight=None):
        # Params:
        # val, row_id_var, col_id_var: numpy arrays

        if weight is None:
            weight = np.ones(val.size)

        self.n_success = val
        self.n_trial = weight.astype(np.float) # PolyaGamma class requires 'float'.
        self.rand_pg = pypolyagamma.PyPolyaGamma()

        self.kappa_coo, self.row_id_map, self.col_id_map \
            = self.prepare_matrix(self.n_success - self.n_trial / 2, row_id_var, col_id_var)

        self.num_factor = num_factor
        self.prior_param = {
            'col_bias_scale': bias_scale,
            'row_bias_scale': bias_scale,
            'factor_scale': factor_scale,
            'weight': weight,
            'df': 5.0,
        }

    def prepare_matrix(self, val, row_id_var, col_id_var):
        # Takes a vector of observed values and two categorical variables
        # and returns a sparse matrix in coo format.

        row_id = np.unique(row_id_var) 
        col_id = np.unique(col_id_var)
        nrow = row_id.size
        ncol = col_id.size

        # Associate each of the unique id names to a row and column index.
        row_id_map = {row_id[index]: index for index in range(len(row_id))}
        col_id_map = {col_id[index]: index for index in range(len(col_id))}

        row_indices = np.array([row_id_map[id] for id in row_id_var])
        col_indices = np.array([col_id_map[id] for id in col_id_var])
        y_coo = scipy.sparse.coo_matrix((val, (row_indices, col_indices)), shape=(nrow, ncol))

        return y_coo, row_id_map, col_id_map

    def compute_logp(self, mu, r, u, c, v):
        # This function computes the log posterior probability (with the weight
        # parameter marginalized out).
        loglik = np.sum(self.n_success * mu) - np.sum(self.n_trial * np.log(1 + np.exp(mu)))
        logp_prior = \
            - self.prior_param['col_bias_scale'] ** -2 / 2 * np.sum(c ** 2) + \
            - self.prior_param['factor_scale'] ** -2 / 2 * np.sum(v ** 2, (0, 1)) + \
            - self.prior_param['row_bias_scale'] ** -2 / 2 * np.sum(r ** 2) + \
            - self.prior_param['factor_scale'] ** -2 / 2 * np.sum(u ** 2, (0, 1))
        return loglik + logp_prior

    def compute_model_mean(self, I, J, mu0, r, u, c, v):
        # Params:
        # I - row indices
        # J - column indices
        return mu0 + r[I] + c[J] + np.sum(u[I,:] * v[J,:], 1)

    def compute_model_mean_sample(self, row_id, col_id, sample_dict, burnin=0):
        # Params:
        # burnin - the number of samples to discard.
        #
        # Returns:
        # mu_sample - numpy array whose second axis corresponds to posterior samples
        #   of model mean.
        row_exist = np.array([(id in self.row_id_map) for id in row_id])
        col_exist = np.array([(id in self.col_id_map) for id in col_id])
        was_matched = np.logical_and(row_exist, col_exist)
        I = np.array([self.row_id_map[row_id[i]] for i in range(len(row_id)) if was_matched[i]])
        J = np.array([self.col_id_map[col_id[j]] for j in range(len(col_id)) if was_matched[j]])

        mu_sample = np.zeros((len(row_id), len(sample_dict['mu0'][burnin:])))
        mu_sample[~was_matched, :] = np.nan
        mu_sample[was_matched, :] = \
            np.tile(sample_dict['mu0'][burnin:], (len(I), 1)) + \
            sample_dict['r'][I, burnin:] + sample_dict['c'][J, burnin:] + \
            np.sum(sample_dict['u'][I, :, burnin:] * sample_dict['v'][J, :, burnin:], 1)

        if not np.all(was_matched):
            print('Only {:.3g} percent of the categories could be matched with the trained matrix'.format(
                100 * np.mean(was_matched)
            ))
            print("NaN indicates the unmatched rows.")

        return mu_sample

    def gibbs(self, n_burnin, n_mcmc, n_update=100, num_process=1, y_test_coo=None, weight_test=None, seed=None, relaxation=-0.0):

        np.random.seed(seed)
        self.relaxation = relaxation  # Recovers the standard Gibbs sampler when relaxation = 0.

        n_iter_per_update = max(1, math.floor((n_burnin + n_mcmc) / n_update))
        nrow, ncol = self.kappa_coo.shape

        # Pre-allocate
        logp_samples = np.zeros(n_burnin + n_mcmc)
        mu0_samples = np.zeros(n_mcmc)
        c_samples = np.zeros((ncol, n_mcmc))
        v_samples = np.zeros((ncol, self.num_factor, n_mcmc))
        r_samples = np.zeros((nrow, n_mcmc))
        u_samples = np.zeros((nrow, self.num_factor, n_mcmc))
        post_mean_mu = np.zeros(self.kappa_coo.nnz)

        # These variables are used only if y_test_coo is not None.
        y_pred = 0
        y_pred_post_mean = 0
        rmse_samples = np.zeros(n_burnin + n_mcmc)
        if (y_test_coo is not None) and (weight_test is None):
            weight_test = np.ones(y_test_coo.data.size)

        # Initial value
        mu = np.zeros(self.kappa_coo.nnz)
        mu0 = 0
        r = np.zeros(nrow)
        u = np.zeros((nrow, self.num_factor))
        c = np.zeros(ncol)
        v = np.zeros((ncol, self.num_factor))
        phi = self.prior_param['weight'] / 4

        # Gibbs steps
        for i in range(n_burnin + n_mcmc):

            mu, mu0, r, u, c, v, phi = \
                self.gibbs_onepass(mu, mu0, r, u, c, v, phi, num_process)

            logp_samples[i] = self.compute_logp(mu, r, u, c, v)
            if y_test_coo is not None:
                y_pred = self.compute_model_mean(y_test_coo.row, y_test_coo.col, mu0, r, u, c, v)
                rmse_samples[i] = math.sqrt(np.mean(weight_test * (y_pred - y_test_coo.data) ** 2))

            if ((i + 1) % n_iter_per_update) == 0:
                print('{:d} iterations have been completed.'.format(i + 1))
                print('The total increase in log posterior so far is {:.3g}.'.format(logp_samples[i] - logp_samples[0]))
                if y_test_coo is not None:
                    print('The prediction error with the current parameter estimates is {:.3g}.'.format(rmse_samples[i]))
                    if i >= n_burnin:
                        test_err = math.sqrt(np.mean(weight_test * (y_test_coo.data - y_pred_post_mean) ** 2))
                        print('The prediction error by the averaged estimate is {:.3g}.'.format(test_err))

            if i >= n_burnin:
                index = i - n_burnin
                mu0_samples[index] = mu0
                c_samples[:, index] = c
                u_samples[:, :, index] = u
                r_samples[:, index] = r
                v_samples[:, :, index] = v
                post_mean_mu = index / (index + 1) * post_mean_mu + 1 / (index + 1) * mu
                y_pred_post_mean = index / (index + 1) * y_pred_post_mean + 1 / (index + 1) * y_pred

        # Save outputs
        sample_dict = {
            'logp': logp_samples,
            'mu0': mu0_samples,
            'r': r_samples,
            'u': u_samples,
            'c': c_samples,
            'v': v_samples,
        }
        if y_test_coo is not None:
            sample_dict['rmse'] = rmse_samples

        return post_mean_mu, sample_dict

    def gibbs_onepass(self, mu, mu0, r, u, c, v, phi, num_process):

        y = self.kappa_coo.data / phi
        self.y_csr = scipy.sparse.csr_matrix((y, (self.kappa_coo.row, self.kappa_coo.col)), self.kappa_coo.shape)
        self.y_csc = scipy.sparse.csc_matrix((y, (self.kappa_coo.row, self.kappa_coo.col)), self.kappa_coo.shape)

        mu0 = self.update_intercept(y, phi, mu - mu0)
        phi_csr = scipy.sparse.csr_matrix((phi, (self.kappa_coo.row, self.kappa_coo.col)), self.kappa_coo.shape)
        r, u = self.update_row_param(phi_csr, mu0, c, v, r, u, num_process)
        phi_csc = scipy.sparse.csc_matrix((phi, (self.kappa_coo.row, self.kappa_coo.col)), self.kappa_coo.shape)
        c, v = self.update_col_param(phi_csc, mu0, r, u, c, v, num_process)
        phi, mu = self.update_weight_param(mu0, r, u, c, v)

        return mu, mu0, r, u, c, v, phi


    def update_intercept(self, y, phi, mu_wo_intercept):

        post_prec = np.sum(phi)
        residual = y - mu_wo_intercept
        post_mean = np.sum(phi * residual) / post_prec
        mu0 = np.random.normal(post_mean, 1 / math.sqrt(post_prec))
        return mu0

    def update_weight_param(self, mu0, r, u, c, v):
        # Returns the weight parameters in an 1-D array in the row major order
        # and also the mean estimate of matrix factorization as a by-product.

        mu = self.compute_model_mean(self.kappa_coo.row, self.kappa_coo.col, mu0, r, u, c, v)
        shape = self.n_trial
        tilt = mu
        omega = np.zeros(len(mu))
        self.rand_pg.pgdrawv(shape, tilt, omega)

        return omega, mu

    def update_row_param(self, phi_csr, mu0, c, v, r_prev, u_prev, num_process):

        nrow = self.y_csr.shape[0]

        # Update 'c' and 'v' block-wise in parallel.
        if num_process == 1:
            r, u = self.update_row_param_blockwise(self.y_csr, phi_csr, mu0, c, v, r_prev, u_prev)
        else:
            n_block = num_process
            block_ind = np.linspace(0, nrow, 1 + n_block, dtype=int)
            ru = joblib.Parallel(n_jobs=num_process)(
                joblib.delayed(self.update_row_param_blockwise)(self.y_csr[block_ind[m]:block_ind[m + 1], :],
                                                   phi_csr[block_ind[m]:block_ind[m + 1], :],
                                                   mu0, c, v,
                                                   r_prev[block_ind[m]:block_ind[m + 1]],
                                                   u_prev[block_ind[m]:block_ind[m + 1]])
                for m in range(n_block))
            r = np.concatenate([ru_i[0] for ru_i in ru])
            u = np.vstack([ru_i[1] for ru_i in ru])

        return r, u

    def update_row_param_blockwise(self, y_csr, phi_csr, mu0, c, v, r_prev, u_prev):

        nrow = y_csr.shape[0]
        prior_Phi = np.diag(np.hstack((self.prior_param['row_bias_scale'] ** -2,
                                       np.tile(self.prior_param['factor_scale'] ** -2, self.num_factor))))
        indptr = y_csr.indptr
        ru = [self.update_per_row(y_csr.data[indptr[i]:indptr[i+1]],
                                  phi_csr.data[indptr[i]:indptr[i+1]],
                                  y_csr.indices[indptr[i]:indptr[i+1]],
                                  mu0, c, v, r_prev[i], u_prev[i,:], prior_Phi) for i in range(nrow)]
        r = np.array([ru_i[0] for ru_i in ru])
        u = np.vstack([ru_i[1] for ru_i in ru])

        return r, u

    def update_per_row(self, y_i, phi_i, J, mu0, c, v, r_prev_i, u_prev_i, prior_Phi):
        # Params:
        #   J - column indices

        nnz_i = len(J)
        residual_i = y_i - mu0 - c[J]
        v_T = np.hstack((np.ones((nnz_i, 1)), v[J, :]))
        post_Phi_i = prior_Phi + \
                     np.dot(v_T.T,
                            np.tile(phi_i[:, np.newaxis], (1, 1 + self.num_factor)) * v_T)  # Weighted sum of v_j * v_j.T
        post_mean_i = np.squeeze(np.dot(phi_i * residual_i, v_T))
        C, lower = scipy.linalg.cho_factor(post_Phi_i)
        post_mean_i = scipy.linalg.cho_solve((C, lower), post_mean_i)
        # Generate Gaussian, recycling the Cholesky factorization from the posterior mean computation.
        ru_i = math.sqrt(1 - self.relaxation ** 2) * scipy.linalg.solve_triangular(C, np.random.randn(len(post_mean_i)),
                                                                                   lower=lower)
        ru_i += post_mean_i + self.relaxation * (post_mean_i - np.concatenate(([r_prev_i], u_prev_i)))
        r_i = ru_i[0]
        u_i = ru_i[1:]

        return r_i, u_i

    def update_col_param(self, phi_csc, mu0, r, u, c_prev, v_prev, num_process):

        ncol = self.y_csc.shape[1]

        if num_process == 1:
            c, v = self.update_col_param_blockwise(self.y_csc, phi_csc, mu0, r, u, c_prev, v_prev)
        else:
            # Update 'c' and 'v' block-wise in parallel.
            n_block = num_process
            block_ind = np.linspace(0, ncol, 1 + n_block, dtype=int)
            cv = joblib.Parallel(n_jobs=num_process)(
                joblib.delayed(self.update_col_param_blockwise)(self.y_csc[:, block_ind[m]:block_ind[m + 1]],
                                                   phi_csc[:, block_ind[m]:block_ind[m + 1]],
                                                   mu0, r, u,
                                                   c_prev[block_ind[m]:block_ind[m + 1]],
                                                   v_prev[block_ind[m]:block_ind[m + 1]])
                for m in range(n_block))
            c = np.concatenate([cv_j[0] for cv_j in cv])
            v = np.vstack([cv_j[1] for cv_j in cv])

        return c, v

    def update_col_param_blockwise(self, y_csc, phi_csc, mu0, r, u, c_prev, v_prev):

        ncol = y_csc.shape[1]
        prior_Phi = np.diag(np.hstack((self.prior_param['col_bias_scale'] ** -2,
                                       np.tile(self.prior_param['factor_scale'] ** -2, self.num_factor))))

        indptr = y_csc.indptr
        cv = [self.update_per_col(y_csc.data[indptr[j]:indptr[j+1]],
                                  phi_csc.data[indptr[j]:indptr[j+1]],
                                  y_csc.indices[indptr[j]:indptr[j+1]],
                                  mu0, r, u, c_prev[j], v_prev[j,:], prior_Phi) for j in range(ncol)]
        c = np.array([cv_j[0] for cv_j in cv])
        v = np.vstack([cv_j[1] for cv_j in cv])

        return c, v

    def update_per_col(self, y_j, phi_j, I, mu0, r, u, c_prev_j, v_prev_j, prior_Phi):

        nnz_j = len(I)
        residual_j = y_j - mu0 - r[I]
        u_T = np.hstack((np.ones((nnz_j, 1)), u[I, :]))
        post_Phi_j = prior_Phi + \
                     np.dot(u_T.T,
                            np.tile(phi_j[:, np.newaxis], (1, 1 + self.num_factor)) * u_T)  # Weighted sum of u_i * u_i.T
        post_mean_j = np.squeeze(np.dot(phi_j * residual_j, u_T))
        C, lower = scipy.linalg.cho_factor(post_Phi_j)
        post_mean_j = scipy.linalg.cho_solve((C, lower), post_mean_j)
        # Generate Gaussian, recycling the Cholesky factorization from the posterior mean computation.
        cv_j = math.sqrt(1 - self.relaxation ** 2) * scipy.linalg.solve_triangular(C, np.random.randn(len(post_mean_j)),
                                                                              lower=lower)
        cv_j += post_mean_j + self.relaxation * (post_mean_j - np.concatenate(([c_prev_j], v_prev_j)))
        c_j = cv_j[0]
        v_j = cv_j[1:]

        return c_j, v_j



    # Old functions for row and column parameter updates. Saved in case it is easier to cythonize.
    def for_loop_update_row_param_blockwise(self, y_csr, phi_csr, mu0, c, v, r_prev, u_prev):

        nrow = y_csr.shape[0]
        num_factor = v.shape[1]
        prior_Phi = np.diag(np.hstack((self.prior_param['row_bias_scale'] ** -2,
                                       np.tile(self.prior_param['factor_scale'] ** -2, num_factor))))

        # Pre-allocate
        r = np.zeros(nrow)
        u = np.zeros((nrow, num_factor))

        # NOTE: The loop through 'i' is completely parallelizable.
        for i in range(nrow):
            j = y_csr[i, :].indices
            nnz_i = len(j)
            residual_i = y_csr[i, :].data - mu0 - c[j]
            phi_i = phi_csr[i, :].data.copy()

            v_T = np.hstack((np.ones((nnz_i, 1)), v[j, :]))
            post_Phi_i = prior_Phi + \
                         np.dot(v_T.T,
                                np.tile(phi_i[:, np.newaxis], (1, 1 + num_factor)) * v_T)  # Weighted sum of v_j * v_j.T
            post_mean_i = np.squeeze(np.dot(phi_i * residual_i, v_T))

            C, lower = scipy.linalg.cho_factor(post_Phi_i)
            post_mean_i = scipy.linalg.cho_solve((C, lower), post_mean_i)
            # Generate Gaussian, recycling the Cholesky factorization from the posterior mean computation.
            ru_i = math.sqrt(1 - self.relaxation ** 2) * scipy.linalg.solve_triangular(C, np.random.randn(len(post_mean_i)),
                                                                                       lower=lower)
            ru_i += post_mean_i + self.relaxation * (post_mean_i - np.concatenate(([r_prev[i]], u_prev[i, :])))
            r[i] = ru_i[0]
            u[i, :] = ru_i[1:]

        return r, u


    def for_loop_update_col_param_blockwise(self, y_csc, phi_csc, mu0, r, u, c_prev, v_prev):

        ncol = y_csc.shape[1]
        num_factor = u.shape[1]
        prior_Phi = np.diag(np.hstack((self.prior_param['col_bias_scale'] ** -2,
                                       np.tile(self.prior_param['factor_scale'] ** -2, num_factor))))

        # Pre-allocate
        c = np.zeros(ncol)
        v = np.zeros((ncol, num_factor))

        # NOTE: The loop through 'j' is completely parallelizable.
        for j in range(ncol):
            i = y_csc[:, j].indices
            nnz_j = len(i)
            residual_j = y_csc[:, j].data - mu0 - r[i]
            phi_j = phi_csc[:, j].data

            u_T = np.hstack((np.ones((nnz_j, 1)), u[i, :]))
            post_Phi_j = prior_Phi + \
                         np.dot(u_T.T,
                                np.tile(phi_j[:, np.newaxis], (1, 1 + num_factor)) * u_T)  # Weighted sum of u_i * u_i.T
            post_mean_j = np.squeeze(np.dot(phi_j * residual_j, u_T))

            C, lower = scipy.linalg.cho_factor(post_Phi_j)
            post_mean_j = scipy.linalg.cho_solve((C, lower), post_mean_j)
            # Generate Gaussian, recycling the Cholesky factorization from the posterior mean computation.
            cv_j = math.sqrt(1 - self.relaxation ** 2) * scipy.linalg.solve_triangular(C, np.random.randn(len(post_mean_j)),
                                                                                       lower=lower)
            cv_j += post_mean_j + self.relaxation * (post_mean_j - np.concatenate(([c_prev[j]], v_prev[j, :])))
            c[j] = cv_j[0]
            v[j, :] = cv_j[1:]

        return c, v