"""Evaluate a model on the data"""

from pathlib import Path
import torch

# Do avoid a need for changing Evironmental Variables outside of this script
import os,sys,inspect
currentdir = os.path.dirname(os.path.realpath(__file__) )
parentdir = os.path.dirname(currentdir)
sys.path.insert(0,parentdir) 

# My modules
import customconfig
import data
import metrics
import nets
from trainer import Trainer
from experiment import WandbRunWrappper

# --------------- from experimnet ---------
system_info = customconfig.Properties('./system.json')
experiment = WandbRunWrappper(
    system_info['wandb_username'],
    project_name='Garments-Reconstruction', 
    run_name='all-new-300-server', 
    run_id='22n3425w')  # finished experiment

if not experiment.is_finished():
    print('Warning::Evaluating unfinished experiment')

# -------- data -------
dataset_list = [
        'test_150_jacket_hood_sleeveless_210331-11-16-33',
        'test_150_skirt_waistband_210331-16-05-37', 
        # 'test_150_tee_hood_210401-15-25-29',
        'test_150_jacket_sleeveless_210331-15-54-26',
        # 'test_150_dress_210401-17-57-12',
        # 'test_150_jumpsuit_210401-16-28-21',
        'test_150_wb_jumpsuit_sleeveless_210404-11-27-30'
    ]

# data_config also contains the names of datasets to use
split, batch_size, data_config = experiment.data_info()  # note that run is not initialized -- we use info from finished run

data_config.update({'obj_filetag': 'sim'})  # sim\scan imitation stats
data_config.update(data_folders=dataset_list)
split.update(filename=None, test_percent=100, valid_percent=0)

dataset = data.Garment3DPatternFullDataset(
    system_info['datasets_path'], data_config, gt_caching=True, feature_caching=True)

# print(dataset.config)
# print('Batch: {}, Split: {}'.format(batch_size, split))

datawrapper = data.DatasetWrapper(dataset, batch_size=batch_size)  # no split given -- evaluating on the full loaded dataset!!

# ----- Model architecture -----
model = nets.GarmentFullPattern3D(dataset.config, experiment.NN_config())

model.load_state_dict(experiment.load_best_model(device='cuda:0')['model_state_dict'])

# ------- Evaluate --------
# loss = metrics.eval_metrics(model, datawrapper, 'full')
# print('Full metrics on unseen set: {}'.format(loss))
# breakdown = metrics.eval_metrics(model, datawrapper, 'full_per_data_folder')
# print('Metrics per dataset: {}'.format(breakdown))

# ---------- Log to the experiment -----------

# experiment.add_statistic('unseen_full', test_metrics)
# experiment.add_statistic('unseen', test_breakdown)
# experiment.add_statistic('unseen_fodlers', dataset_list)

# -------- Predict ---------
# save predictions to file
prediction_path = datawrapper.predict(model, save_to=Path(system_info['output']), sections=['full'])
print('Saved to {}'.format(prediction_path))
# # reflect predictions info in expetiment
# experiment.add_statistic('unseen_pred_folder', prediction_path.name)

# art_name = 'multi-data-unseen' if len(datawrapper.dataset.data_folders) > 1 else datawrapper.dataset.data_folders[0] + '-unseen'
# experiment.add_artifact(prediction_path, art_name, 'result')