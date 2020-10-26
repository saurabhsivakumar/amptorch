import copy
import sys
import os
import random
import numpy as np

import torch

import ase.db
from ase.calculators.calculator import Calculator
from ase.calculators.singlepoint import SinglePointCalculator as sp


import skorch
from skorch import NeuralNetRegressor
from skorch.dataset import CVSplit
from skorch.callbacks import Checkpoint, EpochScoring

from amptorch.gaussian import SNN_Gaussian
from amptorch.skorch_model import AMP
from amptorch.skorch_model.utils import (
    target_extractor,
    energy_score,
    forces_score,
    train_end_load_best_loss,
)
from amptorch.data_preprocess import AtomsDataset, collate_amp
from amptorch.model import BPNN, CustomMSELoss
from amptorch.delta_models.morse import morse_potential
from amptorch.active_learning.al_utils import write_to_db,CounterCalc
from amptorch.active_learning.trainer import train_calcs
from amptorch.active_learning.ensemble_calc import make_fps
from amptorch.active_learning.bootstrap import bootstrap_ensemble
from amptorch.active_learning.query_methods import termination_criteria,neb_query

import matplotlib.pyplot as plt

__author__ = "Muhammed Shuaibi"
__email__ = "mshuaibi@andrew.cmu.edu"


class AtomisticActiveLearner:
    """Active Learner
    Parameters
    ----------
     training_data: list
        List of Atoms objects representing the initial dataset.
    training_params: dict
        Dictionary of training parameters and model settings.
    parent_calc: object.
        Calculator to be used for querying calculations.
    ensemble: boolean.
        Whether to train an ensemble of models to make predictions. ensemble
        must be True if uncertainty based query methods are to be used.
     """

    implemented_properties = ["energy", "forces"]

    def __init__(self, training_data, training_params, parent_calc, convergence_func, ensemble=False):
        self.training_data = copy.deepcopy(training_data)
        self.training_params = training_params
        self.parent_calc = CounterCalc(parent_calc,'parent_learner')
        self.ensemble = ensemble
        self.parent_calls = 0
        self.iteration = 0
        self.convergence_func = convergence_func

        if ensemble:
            assert isinstance(ensemble, int) and ensemble > 1, "Invalid ensemble!"
            self.training_data, self.parent_dataset = bootstrap_ensemble(
                self.training_data, n_ensembles=ensemble
            )

            # make initial fingerprints - daemonic processes cannot do this
            make_fps(training_data, training_params["Gs"])
        else:
            self.parent_dataset = self.training_data

    def learn(self, atomistic_method, query_strategy):
        al_convergence = self.training_params["al_convergence"]
        samples_to_retrain = self.training_params["samples_to_retrain"]
        filename = self.training_params["filename"]
        file_dir = self.training_params["file_dir"]
        queries_db = ase.db.connect("{}.db".format(filename))
        os.makedirs(file_dir, exist_ok=True)
        convergence_criteria_list = []
        f_terminate = False
        method = al_convergence["method"]
        while not f_terminate:
            fn_label = f"{file_dir}{filename}_iter_{self.iteration}"
            # active learning random scheme
            if self.iteration > 0 and method != 'neb_iter':
                queried_images = query_strategy(
                    self.parent_dataset,
                    sample_candidates,
                    samples_to_retrain,
                    parent_calc=self.parent_calc,
                )
                write_to_db(queries_db, queried_images)
                self.parent_dataset, self.training_data = self.add_data(queried_images)
                self.parent_calls += len(queried_images)

            # train ml calculator
            trained_calc = train_calcs(
                training_data=self.training_data,
                training_params=self.training_params,
                ensemble=self.ensemble,
                ncores=self.training_params["cores"],
            )
            # run atomistic_method using trained ml calculator
            atomistic_method.run(calc=trained_calc, filename=fn_label)
            # collect resulting trajectory files
            sample_candidates = atomistic_method.get_trajectory(filename=fn_label)

            #FOR NEBs:
            if method == "neb_iter":
              samples_index = atomistic_method.intermediate_samples + 2
              sample_candidates = sample_candidates[-samples_index:]
              ml2relax = atomistic_method.ml2relax
            self.iteration += 1

            # criteria to stop active learning
            # TODO Find a better way to structure this.
            
            if method == "iter":
                termination_args = {
                    "current_i": self.iteration,
                    "total_i": al_convergence["num_iterations"],
                    "images": sample_candidates,
                    "calc": self.parent_calc,
                    "energy_tol": al_convergence["energy_tol"],
                    "convergence_check": al_convergence["convergence_check"]
                }
                terminate_list = termination_criteria(method=method, termination_args=termination_args,convergence_func = self.convergence_func)
                terminate = terminate_list[0]
                convergence_criteria_list.append(terminate_list[1])
                if al_convergence["convergence_check"] == True:
                    self.parent_calls += 1
                    tested_image = terminate_list[2]
                    self.parent_dataset, self.training_data = self.add_data(tested_image)

            elif method == "final":
                termination_args = {
                    "images": sample_candidates,
                    "calc": self.parent_calc,
                    "energy_tol": al_convergence["energy_tol"],
                    "force_tol": al_convergence["force_tol"],
                }
                self.parent_calls += 1
                terminate_list = termination_criteria(method=method, termination_args=termination_args)
                terminate = terminate_list[0]
                convergence = terminate_list[1]
                convergence_criteria_list.append(convergence)
                
            elif method == "neb_iter":
                termination_args = {
                    "current_i": self.iteration,
                    "total_i": al_convergence["num_iterations"],
                    "images": sample_candidates,
                    "calc": self.parent_calc,
                    "samples_to_retrain": samples_to_retrain,
                    "energy_tol": al_convergence["energy_tol"],
                    "ml2relax": ml2relax
                }
                terminate_list = neb_query(method=method, termination_args=termination_args)
                terminate = terminate_list[0]
                e_convergence = terminate_list[1]
                queried_images = terminate_list[2]
                convergence_criteria_list.append(e_convergence)
                write_to_db(queries_db, queried_images)
                self.parent_dataset, self.training_data = self.add_data(queried_images)
                self.parent_calls += len(queried_images)
                
            f_terminate = terminate

        if self.iteration >= al_convergence["num_iterations"]:
          print('Terminating! Total number of iterations reached')
        else:
          print('Terminating! Convergence criteria has been met')
          
        self.convergence_plot(convergence_criteria_list)
        print(convergence_criteria_list)
        return trained_calc,self.iteration

    def add_data(self, queried_images):
        if self.ensemble:
            for query in queried_images:
                self.training_data, self.parent_dataset = bootstrap_ensemble(
                    self.parent_dataset,
                    self.training_data,
                    query,
                    n_ensembles=self.ensemble,
                )
        else:
            self.training_data += queried_images
        return self.parent_dataset, self.training_data
    
    def convergence_plot(self,criteria):
      x_axis = np.arange(0,self.iteration,1)
      plt.semilogy(x_axis,criteria)
      plt.title('convergence plot')
      plt.xlabel('Iterations')
      plt.ylabel('Convergence Condition')
      plt.savefig('convergence plot.png')
      plt.show()
