from __future__ import division
import matplotlib
matplotlib.use('Agg')
import os
import matplotlib.pyplot as plt
import csv
import numpy as np
from numpy.lib.recfunctions import append_fields
from scipy import stats, integrate
import working_functions as wk
import mete
import mete_distributions
import mete_agsne as agsne
import macroecotools as mtools
import macroeco_distributions as md
import multiprocessing

class ssnt_isd_bounded():
    """The individual-size distribution predicted by SSNT.
    
    Diameter is assumed to be lower-bounded at 1, to be consistent
    with METE.
    SSNT is applied on diameter transformed with an arbitrary
    power alpha, i.e., it is assumed that g(D^alpha) is constant.
    The predicted distribution is transformed back to diameter.
    
    """
    def __init__(self, alpha, par):
        """par is the parameter for the lower-truncated exponential
        
        distribution when the scale is D^alpha. 
        The MLE of par is N / (sum(D^alpha) - N) from data.
        
        """
        self.alpha = alpha
        self.par = par
        self.a = 1 # lower bound
        
    def pdf(self, x):
        if x < self.a: return 0
        else: return self.par * self.alpha * np.exp(-self.par * (x ** self.alpha - 1)) * (x ** (self.alpha - 1))
    
    def cdf(self, x): # cdf of D is equal to cdf of D^alpha
        if x < self.a: return 0
        else: return 1 - np.exp(-self.par * (x ** self.alpha - 1))
    
    def ppf(self, q):
        return (1 - np.log(1 - q) / self.par) ** (1 / self.alpha)
    
    def expected(self):  # Note that this is the expected value of D
        ans = integrate.quad(lambda x: x * self.pdf(x), self.a, np.inf)[0]
        return ans
    
    def expected_square(self):
        ans = integrate.quad(lambda x: x ** 2 * self.pdf(x), self.a, np.inf)[0]
        return ans        

def import_likelihood_data(file_name, file_dir = './out_files/'):
    """Import file with likelihood for METE, SSNT, and transformed SSNT"""
    data = np.genfromtxt(file_dir + file_name, dtype = 'S15, S15, f15, f15, f15', 
                         names = ['study', 'site', 'METE', 'SSNT', 'SSNT_transform'], delimiter = ' ')
    return data

def clean_data_agsne(raw_data_site, cutoff_genera = 4, cutoff_sp = 9, max_removal = 0.1):
    """Further cleanup of data, removing individuals with undefined genus. 
    
    Inputs:
    raw_data_site - structured array generated by wk.import_raw_data(), with three columns 'site', 'sp', and 'dbh', for a single site
    min_genera - minimal number of genera required for analysis
    min_sp - minimal number of species
    max_removal - the maximal proportion of individuals removed with undefined genus
    
    Output:
    a structured array with four columns 'site', 'sp', 'genus', and 'dbh'
    
    """
    counter = 0
    genus_list = []
    row_to_remove = []
    for i, row in enumerate(raw_data_site):
        sp_split = row['sp'].split(' ')
        genus = sp_split[0]
        if len(sp_split) > 1 and genus[0].isupper() and genus[1].islower() and (not any(char.isdigit() for char in genus)):
            genus_list.append(genus)
        else: 
            row_to_remove.append(i)
            counter += 1
    if counter / len(raw_data_site) <= max_removal:
        raw_data_site = np.delete(raw_data_site, np.array(row_to_remove), axis = 0)
        gen_col = np.array(genus_list)
        out = append_fields(raw_data_site, 'genus', gen_col, usemask = False)
        if len(np.unique(out['sp'])) > cutoff_sp and len(np.unique(out['genus'])) > cutoff_genera: 
            return out
        else: return None
    else: return None
  
def get_GSNE(raw_data_site):
    """Obtain the state variables given data for a single site, returned by clean_data_genera()."""
    G = len(np.unique(raw_data_site['genus']))
    S = len(np.unique(raw_data_site['sp']))
    N = len(raw_data_site)
    E = sum((raw_data_site['dbh'] / min(raw_data_site['dbh'])) ** 2)
    return G, S, N, E
    
def lik_sp_abd_dbh_ssnt(sad_par, isd_dist, n, dbh_list, log = True):
    """Probability of a species having abundance n and its individuals having dbh [d1, d2, ..., d_n] in SSNT
    
    Inputs:
    sad_par - parameter of the predicted SAD (untruncated logseries)
    isd_dist - predicted distribution of the ISD
    n - abundance
    dbh_list - a list or array of length n with scaled dbh values
    """
    p_sad_log = stats.logser.logpmf(n, sad_par)
    p_dbh = [isd_dist.pdf(dbh) for dbh in dbh_list]
    if log: return p_sad_log + sum([np.log(p_ind) for p_ind in p_dbh])
    else: 
        p_iisd = 1
        for p_ind in p_dbh: p_iisd *= p_ind
        return np.exp(p_sad_log) * p_iisd
    
def lik_sp_abd_dbh_mete(sad_par, sad_upper, iisd_dist, n, dbh_list, log = True):
    """Probability of a species having abundance n and its individuals having dbh [d1, d2, ..., d_n] in METE
    
    Here unlike SSNT, P(d|n) is not equal to the ISD f(d). 
    Inputs:
    sad_par - parameter of the predicted SAD (upper-truncated logseries)
    sad_upper - upper bounded of the predicted SAD
    isd_dist - predicted iISD given n (theta_epsilon)
    n - abundance
    dbh_list - a list or array of length n with scaled dbh values
    """
    p_sad_log = md.trunc_logser.logpmf(n, sad_par, sad_upper)
    p_dbh_log = [iisd_dist.logpdf(dbh ** 2, n) + np.log(2 * dbh) for dbh in dbh_list] # Prediction of METE has to be transformed back to distribution of dbh
    if log: return p_sad_log + sum(p_dbh_log)
    else: 
        p_iisd = 1
        for p_ind in p_dbh_log: p_iisd *= np.exp(p_ind)
        return np.exp(p_sad_log) * p_iisd

def get_obs_pred_sad(raw_data_site, dataset_name, model, out_dir = './out_files/'):
    """Write the observed and predicted RAD to file for a given model.
    
    Inputs:
     raw_data_site - data in the same format as obtained by clean_data_genera(), with
        four columns site, sp, dbh, and genus, and only for one site.
    dataset_name - name of the dataset for raw_data_site.
    model - can take one of three values 'ssnt', 'asne', or 'agsne'. Note that the predicted SAD for SSNT does not 
        change with alternative scaling of D.
    out_dir - directory for output file.
    
    """
    G, S, N, E = get_GSNE(raw_data_site)
    if model == 'ssnt': 
        pred = mete.get_mete_rad(S, N, version = 'untruncated')[0]
    elif model == 'asne': 
        pred = mete.get_mete_rad(S, N)[0]
    elif model == 'agsne': 
        pred = agsne.get_mete_agsne_rad(G, S, N, E)
    obs = np.sort([len(raw_data_site[raw_data_site['sp'] == sp]) for sp in np.unique(raw_data_site['sp'])])[::-1]
    results = np.zeros((S, ), dtype = ('S15, i8, i8'))
    results['f0'] = np.array([raw_data_site['site'][0]] * S)
    results['f1'] = obs
    results['f2'] = pred    
    
    if model == 'ssnt': 
        f1_write = open(out_dir + dataset_name + '_obs_pred_rad_ssnt_0.csv', 'ab')
        f2_write = open(out_dir + dataset_name + '_obs_pred_rad_ssnt_1.csv', 'ab')
        f2 = csv.writer(f2_write)
        f2.writerows(results)
        f2_write.close()
    else: f1_write = open(out_dir + dataset_name + '_obs_pred_rad_' + model + '.csv', 'ab')
    f1 = csv.writer(f1_write)
    f1.writerows(results)
    f1_write.close()

def get_mete_pred_isd_approx(S, N, E):
    """Obtain the dbh2 for N individuals predicted by METE, using the newly derived approximated ISD."""
    psi_appox = mete_distributions.psi_epsilon_approx(S, N, E)
    scaled_rank = [(x + 0.5) / N for x in range(N)]
    pred = np.array([psi_appox.ppf(q) for q in scaled_rank])
    return np.array(pred)

def get_obs_pred_isd(raw_data_site, dataset_name, model, out_dir = './out_files/'):
    """Write the observed and predicted ISD to file for a given model.
    
    Inputs:
     raw_data_site - data in the same format as obtained by clean_data_genera(), with
        four columns site, sp, dbh, and genus, and only for one site.
    dataset_name - name of the dataset for raw_data_site.
    model - can take one of four values 'ssnt_0' (constant growth of diameter D), 
        'ssnt_1' (constant growth of D^2/3), 'asne', or 'agsne'. 
    out_dir - directory for output file.
    
    """
    G, S, N, E = get_GSNE(raw_data_site)
    if model == 'asne':  # Note both ASNE and AGSNE return values in diameter^2, which needs to be transformed back
        pred = get_mete_pred_isd_approx(range(1, N + 1), S, N, E) ** 0.5
    elif model == 'agsne': 
        pred = np.array(agsne.get_mete_agsne_isd(G, S, N, E)) ** 0.5
    else: 
        dbh_scaled = np.array(raw_data_site['dbh'] / min(raw_data_site['dbh']))
        if model == 'ssnt_0': alpha = 1
        elif model == 'ssnt_1': alpha = 2/3
        par = N / (sum(dbh_scaled ** alpha) - N)
        scaled_rank = [(x + 0.5) / N for x in range(N)]
        isd_ssnt = ssnt_isd_bounded(alpha, par)
        pred = np.array([isd_ssnt.ppf(q) for q in scaled_rank])
        
    obs = np.sort(raw_data_site['dbh'] / min(raw_data_site['dbh']))
    results = np.zeros((N, ), dtype = ('S15, f8, f8'))
    results['f0'] = np.array([raw_data_site['site'][0]] * N)
    results['f1'] = obs
    results['f2'] = pred    
    
    f1_write = open(out_dir + dataset_name + '_obs_pred_isd_' + model + '.csv', 'ab')
    f1 = csv.writer(f1_write)
    f1.writerows(results)
    f1_write.close()

def get_obs_pred_sdr(raw_data_site, dataset_name, model, out_dir = './out_files/'):
    """Write the observed and predicted SDR (in unit of D^2) to file for a given model.
    
    Inputs:
     raw_data_site - data in the same format as obtained by clean_data_genera(), with
        four columns site, sp, dbh, and genus, and only for one site.
    dataset_name - name of the dataset for raw_data_site.
    model - can take one of four values 'ssnt_0' (constant growth of diameter D), 
        'ssnt_1' (constant growth of D^2/3), 'asne', or 'agsne'. 
    out_dir - directory for output file.
    
    """
    scaled_d = raw_data_site['dbh'] / min(raw_data_site['dbh'])
    scaled_d2 = scaled_d **2
    G, S, N, E = get_GSNE(raw_data_site)
    lambda1, beta, lambda3 = agsne.get_agsne_lambdas(G, S, N, E)
    theta_agsne = mete_distributions.theta_agsne([G, S, N, E], [lambda1, beta, lambda3, agsne.agsne_lambda3_z(lambda1, beta, S) / lambda3])
    theta_asne = mete_distributions.theta_epsilon(S, N, E)
    if model == 'ssnt_1': alpha = 2/3
    else: alpha = 1
    par = N / (sum(scaled_d ** alpha) - N)
    iisd_ssnt = ssnt_isd_bounded(alpha, par)
   
    pred, obs = [], []
    for sp in np.unique(raw_data_site['sp']):
        n = len(raw_data_site[raw_data_site['sp'] == sp]) # Number of individuals within species
        if model == 'agsne': 
            genus_sp = raw_data_site['genus'][raw_data_site['sp'] == sp][0]
            m = len(np.unique(raw_data_site['sp'][raw_data_site['genus'] == genus_sp])) # Number of specis within genus
            pred.append(theta_agsne.expected(m, n))
        elif model == 'asne': pred.append(theta_asne.E(n))
        elif model in ['ssnt_0', 'ssnt_1']: pred.append(iisd_ssnt.expected_square())
        obs.append(np.mean(scaled_d2[raw_data_site['sp'] == sp]))
    
    results = np.zeros((S, ), dtype = ('S15, f8, f8'))
    results['f0'] = np.array([raw_data_site['site'][0]] * S)
    results['f1'] = obs
    results['f2'] = pred    
    f1_write = open(out_dir + dataset_name + '_obs_pred_sdr_' + model + '.csv', 'ab')
    f1 = csv.writer(f1_write)
    f1.writerows(results)
    f1_write.close()
                
def get_isd_lik_three_models(dat_list, out_dir = './out_files/', cutoff = 9):
    """Function to obtain the community-level log-likelihood (standardized by the number of individuals)
    
    as well as AICc values for METE, SSNT on D, and SSNT on D**(2/3) and write to files. 
    
    """
    for dat_name in dat_list:
        dat = wk.import_raw_data('./data/' + dat_name + '.csv')
        for site in np.unique(dat['site']):
            dat_site = dat[dat['site'] == site]
            S0 = len(np.unique(dat_site['sp']))
            if S0 > cutoff:
                N0 = len(dat_site)
                dbh_scaled = dat_site['dbh'] / min(dat_site['dbh'])
                psi = mete_distributions.psi_epsilon(S0, N0, sum(dbh_scaled ** 2))
                ssnt_isd = ssnt_isd_bounded(1, N0 / (sum(dbh_scaled) - N0))
                ssnt_isd_transform = ssnt_isd_bounded(2/3, N0 / (sum(dbh_scaled ** (2/3)) - N0))
                
                lik_mete, lik_ssnt, lik_ssnt_transform = 0, 0, 0
                for dbh in dbh_scaled:
                    lik_mete += np.log(psi.pdf(dbh ** 2) * 2 * dbh) # psi is on dbh**2
                    lik_ssnt += np.log(ssnt_isd.pdf(dbh))
                    lik_ssnt_transform += np.log(ssnt_isd_transform.pdf(dbh))
                out1 = open(out_dir + 'isd_lik_three_models.txt', 'a')
                print>>out1, dat_name, site, str(lik_mete / N0), str(lik_ssnt / N0), str(lik_ssnt_transform / N0)
                out1.close()
                
                out2 = open(out_dir + 'isd_aicc_three_models.txt', 'a')
                # METE has three parameters (S0, N0, E0) for ISD, while SSNT has two (N0 and sum(dbh**alpha))
                print>>out2, dat_name, site, str(mtools.AICc(lik_mete, 3, N0)), str(mtools.AICc(lik_ssnt, 2, N0)), \
                     str(mtools.AICc(lik_ssnt_transform, 2, N0))
                out2.close()
                
def get_lik_sp_abd_dbh_three_models(dat_list, out_dir = './out_files/', cutoff = 9):
    """Obtain the summed log likelihood of each species having abundance n and its individuals having 
    
    their specific dbh values for the three models METE, SSNT on D, and SSNT on D ** (2/3).
    
    """
    for dat_name in dat_list:
        dat = wk.import_raw_data('./data/' + dat_name + '.csv')
        for site in np.unique(dat['site']):
            dat_site = dat[dat['site'] == site]
            S0 = len(np.unique(dat_site['sp']))
            if S0 > cutoff:
                N0 = len(dat_site)
                dbh_scaled = dat_site['dbh'] / min(dat_site['dbh'])
                theta = mete_distributions.theta_epsilon(S0, N0, sum(dbh_scaled ** 2))
                lambda_mete = np.exp(-mete.get_beta(S0, N0))
                lambda_ssnt = np.exp(-mete.get_beta(S0, N0, version = 'untruncated'))
                ssnt_isd = ssnt_isd_bounded(1, N0 / (sum(dbh_scaled) - N0))
                ssnt_isd_transform = ssnt_isd_bounded(2/3, N0 / (sum(dbh_scaled ** (2/3)) - N0))
                
                lik_mete, lik_ssnt, lik_ssnt_transform = 0, 0, 0
                for sp in np.unique(dat_site['sp']):
                    dbh_sp = dbh_scaled[dat_site['sp'] == sp]
                    n_sp = len(dbh_sp)
                    lik_mete += lik_sp_abd_dbh_mete(lambda_mete, N0, theta, n_sp, dbh_sp)
                    lik_ssnt += lik_sp_abd_dbh_ssnt(lambda_ssnt, ssnt_isd, n_sp, dbh_sp)
                    lik_ssnt_transform += lik_sp_abd_dbh_ssnt(lambda_ssnt, ssnt_isd_transform, n_sp, dbh_sp)
                out = open(out_dir + 'lik_sp_abd_dbh_three_models.txt', 'a')
                print>>out, dat_name, site, str(lik_mete), str(lik_ssnt), str(lik_ssnt_transform)
                out.close()

def plot_obs_pred_diameter(datasets, in_file_name, data_dir = './out_files/', ax = None, radius = 2, mete = False, title = None):
    """Plot the observed vs predicted diamters across multiple datasets. Applies to both ISD and iISD."""
    isd_sites, isd_obs, isd_pred = wk.get_obs_pred_from_file(datasets, data_dir, in_file_name)
    if mete:
        isd_obs = isd_obs ** 0.5
        isd_pred = isd_pred ** 0.5
    if not ax:
        fig = plt.figure(figsize = (3.5, 3.5))
        ax = plt.subplot(111)
    wk.plot_obs_pred(isd_obs, isd_pred, radius, 1, ax = ax)
    ax.set_xlabel('Predicted diameter', labelpad = 4, size = 8)
    ax.set_ylabel('Observed diameter', labelpad = 4, size = 8)
    if title: plt.title(title, fontsize = 10)
    return ax

def plot_obs_pred_sad_sdr(datasets, in_file_name, data_dir = "./out_files/", ax = None, radius =2, title = None, axis_lab = 'abundance'):
    """Plot the observed vs predicted SAD or SDR for each species for multiple datasets."""
    sites, obs, pred = wk.get_obs_pred_from_file(datasets, data_dir, in_file_name)
    if not ax:
        fig = plt.figure(figsize = (3.5, 3.5))
        ax = plt.subplot(111)
    wk.plot_obs_pred(obs, pred, radius, 1, ax = ax)
    ax.set_xlabel('Predicted ' + axis_lab, labelpad = 4, size = 8)
    ax.set_ylabel('Observed ' + axis_lab, labelpad = 4, size = 8)
    if title: plt.title(title, fontsize = 10)
    return ax

def plot_likelihood_comp(lik_1, lik_2, xlabel, ylabel, annotate = True, ax = None):
    """Plot the likelihood two models against each other.
    
    lik_1 and lik_2 are two lists/arrays of the same length, each 
    representing likelihood in each community for one model.
    
    """
    if not ax:
        fig = plt.figure(figsize = (3.5, 3.5))
        ax = plt.subplot(111)
    min_val, max_val = min(list(lik_1) + list(lik_2)), max(list(lik_1) + list(lik_2))
    if min_val < 0: axis_min = 1.1 * min_val
    else: axis_min = 0.9 * min_val
    if max_val < 0: axis_max = 0.9 * max_val
    else: axis_max= 1.1 * max_val
    plt.scatter(lik_1, lik_2, c = '#787878', edgecolors='none')
    plt.plot([axis_min, axis_max], [axis_min, axis_max], 'k-')     
    plt.xlim(axis_min, axis_max)
    plt.ylim(axis_min, axis_max)
    ax.tick_params(axis = 'both', which = 'major', labelsize = 6)
    ax.set_xlabel(xlabel, labelpad = 4, size = 8)
    ax.set_ylabel(ylabel, labelpad = 4, size = 8)
    num_above_line = len([i for i in range(len(lik_1)) if lik_1[i] < lik_2[i]])
    if annotate:
        plt.annotate('Above the line: ' + str(num_above_line) + '/' + str(len(lik_1)), xy = (0.05, 0.85), 
                     xycoords = 'axes fraction', fontsize = 7)
    return ax

def bootstrap_SAD(name_site_combo, model, in_dir = './data/', out_dir = './out_files/', Niter = 200):
    """A general function of bootstrapping for SAD applying to all four models. 
    
    Inputs:
    name_site_combo: a list with dat_name and site
    model - takes one of four values 'ssnt_0', 'ssnt_1', 'asne', or 'agsne'
    in_dir - directory of raw data
    out_dir - directory used both in input (obs_pred.csv file) and output 
    Niter - number of bootstrap samples
    
    Output:
    Writes to disk, with one file for R^2 and one for KS statistic.
    
    """
    dat_name, site = name_site_combo
    dat = wk.import_raw_data(in_dir + dat_name + '.csv')
    dat_site = dat[dat['site'] == site]
    dat_clean = clean_data_agsne(dat_site)    
    G, S, N, E = get_GSNE(dat_clean)
    beta_ssnt = mete.get_beta(S, N, version = 'untruncated')
    beta_asne = mete.get_beta(S, N)
    lambda1, beta, lambda3 = agsne.get_agsne_lambdas(G, S, N, E)
    sad_agsne = mete_distributions.sad_agsne([G, S, N, E], [lambda1, beta, lambda3, agsne.agsne_lambda3_z(lambda1, beta, S) / lambda3])
    dist_for_model = {'ssnt_0': stats.logser(np.exp(-beta_ssnt)), 
                      'ssnt_1': stats.logser(np.exp(-beta_ssnt)), 
                      'asne': md.trunc_logser(np.exp(-beta_asne), N),
                      'agsne': sad_agsne}
    dist = dist_for_model[model]
    pred_obs = wk.import_obs_pred_data(out_dir + dat_name + '_obs_pred_rad_' + model + '.csv')
    pred = pred_obs[pred_obs['site'] == site]['pred'][::-1]
    obs = pred_obs[pred_obs['site'] == site]['obs'][::-1]
    
    out_list_rsquare = [dat_name, site, str(mtools.obs_pred_rsquare(np.log10(obs), np.log10(pred)))]
    emp_cdf = wk.get_obs_cdf(obs)
    out_list_ks = [dat_name, site, str(max(abs(emp_cdf - np.array([dist.cdf(x) for x in obs]))))]
    
    for i in range(Niter):
        if model in ['agsne', 'asne']:
            obs_boot = np.array(sorted(dist.rvs(S)))
            cdf_boot = np.array([dist.cdf(x) for x in obs_boot])
        else:
            cdf_boot = sorted(stats.uniform.rvs(size = S))
            obs_boot = np.array([dist.ppf(x) for x in cdf_boot])
        out_list_rsquare.append(str(mtools.obs_pred_rsquare(np.log10(obs_boot), np.log10(pred))))
        out_list_ks.append(str(max(abs(emp_cdf - np.array(cdf_boot)))))
    
    wk.write_to_file(out_dir + 'SAD_bootstrap_' + model + '_rsquare.txt', ",".join(str(x) for x in out_list_rsquare))
    wk.write_to_file(out_dir + 'SAD_bootstrap_' + model + '_ks.txt', ",".join(str(x) for x in out_list_ks))

def bootstrap_ISD(name_site_combo, model, in_dir = './data/', out_dir = './out_files/', Niter = 200):
    """A general function of bootstrapping for ISD applying to all four models. 
    
    Inputs:
    name_site_combo: a list with dat_name and site
    model - takes one of four values 'ssnt_0', 'ssnt_1', 'asne', or 'agsne'
    in_dir - directory of raw data
    out_dir - directory used both in input (obs_pred.csv file) and output 
    Niter - number of bootstrap samples
    
    Output:
    Writes to disk, with one file for R^2 and one for KS statistic.
    
    """
    dat_name, site = name_site_combo
    dat = wk.import_raw_data(in_dir + dat_name + '.csv')
    dat_site = dat[dat['site'] == site]
    dat_clean = clean_data_agsne(dat_site)    
    G, S, N, E = get_GSNE(dat_clean)
    lambda1, beta, lambda3 = agsne.get_agsne_lambdas(G, S, N, E)
    isd_agsne = mete_distributions.psi_agsne([G, S, N, E], [lambda1, beta, lambda3, agsne.agsne_lambda3_z(lambda1, beta, S) / lambda3])
    isd_asne = mete_distributions.psi_epsilon_approx(S, N, E)
    dbh_scaled = np.array(dat_clean['dbh'] / min(dat_clean['dbh']))
    isd_ssnt_0 = ssnt_isd_bounded(1, N / (sum(dbh_scaled ** 1) - N))
    isd_ssnt_1 = ssnt_isd_bounded(2/3, N / (sum(dbh_scaled ** (2/3)) - N))
    dist_for_model = {'ssnt_0': isd_ssnt_0, 'ssnt_1': isd_ssnt_1, 'asne': isd_asne, 'agsne': isd_agsne}
    dist = dist_for_model[model]
    pred_obs = wk.import_obs_pred_data(out_dir + dat_name + '_obs_pred_isd_' + model + '.csv')
    pred = pred_obs[pred_obs['site'] == site]['pred']
    obs = pred_obs[pred_obs['site'] == site]['obs']
    
    out_list_rsquare = [dat_name, site, str(mtools.obs_pred_rsquare(np.log10(obs), np.log10(pred)))]
    emp_cdf = wk.get_obs_cdf(obs)
    out_list_ks = [dat_name, site, str(max(abs(emp_cdf - np.array([dist.cdf(x) for x in obs]))))]
    
    for i in range(Niter):
            cdf_boot = sorted(stats.uniform.rvs(size = S))
            if model in ['asne', 'agsne']: 
                obs_boot = np.array([dist.ppf(x) for x in cdf_boot]) ** 0.5 # ASNE and AGSNE returns values in D^2 instead of D
            else: obs_boot = np.array([dist.ppf(x) for x in cdf_boot])
        out_list_rsquare.append(str(mtools.obs_pred_rsquare(np.log10(obs_boot), np.log10(pred))))
        out_list_ks.append(str(max(abs(emp_cdf - np.array(cdf_boot)))))
    
    wk.write_to_file(out_dir + 'ISD_bootstrap_' + model + '_rsquare.txt', ",".join(str(x) for x in out_list_rsquare))
    wk.write_to_file(out_dir + 'ISD_bootstrap_' + model + '_ks.txt', ",".join(str(x) for x in out_list_ks))
    
def bootstrap_SDR(name_site_combo, model, in_dir = './data/', out_dir = './out_files/', Niter = 200):
    """A general function of bootstrapping for ISD applying to all four models. 
    
    Inputs:
    name_site_combo: a list with dat_name and site
    model - takes one of four values 'ssnt_0', 'ssnt_1', 'asne', or 'agsne'
    in_dir - directory of raw data
    out_dir - directory used both in input (obs_pred.csv file) and output 
    Niter - number of bootstrap samples
    
    Output:
    Writes to one file on disk for R^2.
    
    """
    dat_name, site = name_site_combo
    dat = wk.import_raw_data(in_dir + dat_name + '.csv')
    dat_site = dat[dat['site'] == site]
    dat_clean = clean_data_agsne(dat_site)    
    G, S, N, E = get_GSNE(dat_clean)
    lambda1, beta, lambda3 = agsne.get_agsne_lambdas(G, S, N, E)
    
    par_list = []
    for sp in np.unique(dat_clean['sp']):
        dat_sp = dat_clean[dat_clean['sp'] == sp]
        n = len(dat_sp)
        m = len(np.unique(dat_sp['genus']))
        par_list.append([m, n])
        
    pred_obs = wk.import_obs_pred_data(out_dir + dat_name + '_obs_pred_sdr_' + model + '.csv')
    pred = pred_obs[pred_obs['site'] == site]['pred']
    obs = pred_obs[pred_obs['site'] == site]['obs'] 
    out_list_rsquare = [dat_name, site, str(mtools.obs_pred_rsquare(np.log10(obs), np.log10(pred)))]
    
    iisd_agsne = mete_distributions.theta_agsne([G, S, N, E], [lambda1, beta, lambda3, agsne.agsne_lambda3_z(lambda1, beta, S) / lambda3])
    iisd_asne = mete_distributions.theta_epsilon(S, N, E)
    dbh_scaled = np.array(dat_clean['dbh'] / min(dat_clean['dbh']))
    iisd_ssnt_0 = ssnt_isd_bounded(1, N / (sum(dbh_scaled ** 1) - N))
    iisd_ssnt_1 = ssnt_isd_bounded(2/3, N / (sum(dbh_scaled ** (2/3)) - N))
    dist_for_model = {'ssnt_0': iisd_ssnt_0, 'ssnt_1': iisd_ssnt_1, 'asne': iisd_asne, 'agsne': iisd_agsne}
    dist = dist_for_model[model]
        
    for i in range(Niter):
        if model in ['ssnt_0', 'ssnt_1']: obs_boot = np.array([np.mean((dist.rvs(par[1])) ** 2) for par in par_list]) # Here par[1] is n for each species
        elif model == 'asne': 
            obs_boot = np.array([np.mean((dist.rvs(par[1], par[1])) ** 2) for par in par_list])
        else:
            obs_boot = np.array([np.mean((dist.rvs(par[0], par[1], par[1])) ** 2) for par in par_list])
        out_list_rsquare.append(str(mtools.obs_pred_rsquare(np.log10(obs_boot), np.log10(pred))))
    
    wk.write_to_file(out_dir + 'ISD_bootstrap_' + model + '_rsquare.txt', ",".join(str(x) for x in out_list_rsquare))
            
def plot_bootstrap(alpha = 1):
    """Similar to create_Fig_E2() in working_functions.
    
    Add input "alpha" to adapt to output files for different transformations.
    
    """
    fig = plt.figure(figsize = (7, 14))
    sad_r2 = wk.import_bootstrap_file('./out_files/SAD_bootstrap_SSNT_rsquare.txt', Niter = 200)
    ax_1 = plt.subplot(421)
    wk.plot_hist_quan(sad_r2, ax = ax_1)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title(r'SAD, $R^2$', fontsize = 10)
 
    sad_ks = wk.import_bootstrap_file('./out_files/SAD_bootstrap_SSNT_ks.txt', Niter = 200)
    ax_2 = plt.subplot(422)
    wk.plot_hist_quan(sad_ks, dat_type = 'ks', ax = ax_2)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title('SAD, K-S Statistic', fontsize = 10)
 
    isd_r2 = wk.import_bootstrap_file('./out_files/ISD_bootstrap_rsquare_' + str(round(alpha, 2)) + '.txt', Niter = 200)
    ax_3 = plt.subplot(423)
    wk.plot_hist_quan(isd_r2, ax = ax_3)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title(r'ISD, $R^2$', fontsize = 10)
 
    isd_ks = wk.import_bootstrap_file('./out_files/ISD_bootstrap_ks_' + str(round(alpha, 2)) + '.txt', Niter = 200)
    ax_4 = plt.subplot(424)
    wk.plot_hist_quan(isd_ks, dat_type = 'ks', ax = ax_4)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title('ISD, K-S Statistic', fontsize = 10)

    iisd_r2 = wk.import_bootstrap_file('./out_files/iISD_bootstrap_rsquare_' + str(round(alpha, 2)) + '.txt', Niter = 200)
    ax_5 = plt.subplot(425)
    wk.plot_hist_quan(iisd_r2, ax = ax_5)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title(r'iISD, $R^2$', fontsize = 10)
   
    ax_6 = plt.subplot(426)
    wk.plot_hist_quan_iisd_ks('./out_files/iISD_bootstrap_ks/SSNT_' + str(round(alpha, 2)) + '/', ax = ax_6)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title('iISD, K-S Statistic', fontsize = 10)
    
    sdr_r2 = wk.import_bootstrap_file('./out_files/SDR_bootstrap_rsquare_' + str(round(alpha, 2)) + '.txt', Niter = 200)
    ax_7 = plt.subplot(427)
    wk.plot_hist_quan(sdr_r2, ax = ax_7)
    plt.xlabel('Quantile', fontsize = 8)
    plt.ylabel('Frequency', fontsize = 8)
    plt.title(r'SDR, $R^2$', fontsize = 10)
   
    plt.subplots_adjust(wspace = 0.29, hspace = 0.29)
    plt.savefig('Bootstrap_SSNT_' + str(round(alpha, 2)) + '_200.pdf', dpi = 600)

