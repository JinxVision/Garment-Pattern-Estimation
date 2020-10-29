import numpy as np
import os
from pathlib import Path
import shutil
import time
from datetime import datetime

import torch
from torch.utils.data import DataLoader, Dataset
import torchvision.transforms as transforms
import igl
# import meshplot  # when uncommented, could lead to problems with wandb run syncing

# My modules
from customconfig import Properties
from pattern.core import ParametrizedPattern, BasicPattern
from pattern.wrappers import VisPattern


# ---------------------- Main Wrapper ------------------
class DatasetWrapper(object):
    """Resposible for keeping dataset, its splits, loaders & processing routines.
        Allows to reproduce earlier splits
    """
    def __init__(self, in_dataset, known_split=None, batch_size=None, shuffle_train=True):
        """Initialize wrapping around provided dataset. If splits/batch_size is known """

        self.dataset = in_dataset
        self.data_section_list = ['full', 'train', 'validation', 'test']

        self.training = in_dataset
        self.validation = None
        self.test = None

        self.batch_size = None
        self.loader_full = None
        self.loader_train = None
        self.loader_validation = None
        self.loader_test = None

        self.split_info = {
            'random_seed': None, 
            'valid_percent': None, 
            'test_percent': None
        }

        if known_split is not None:
            self.load_split(known_split)
        if batch_size is not None:
            self.batch_size = batch_size
            self.new_loaders(batch_size, shuffle_train)
    
    def get_loader(self, data_section='full'):
        """Return loader that corresponds to given data section. None if requested loader does not exist"""
        if data_section == 'full':
            return self.loader_full
        elif data_section == 'train':
            return self.loader_train
        elif data_section == 'test':
            return self.loader_test
        elif data_section == 'validation':
            return self.loader_validation
        
        raise ValueError('DataWrapper::requested loader on unknown data section {}'.format(data_section))

    def new_loaders(self, batch_size=None, shuffle_train=True):
        """Create loaders for current data split. Note that result depends on the random number generator!"""
        if batch_size is not None:
            self.batch_size = batch_size
        if self.batch_size is None:
            raise RuntimeError('DataWrapper:Error:cannot create loaders: batch_size is not set')

        self.loader_train = DataLoader(self.training, self.batch_size, shuffle=shuffle_train)
        self.loader_validation = DataLoader(self.validation, self.batch_size) if self.validation else None
        self.loader_test = DataLoader(self.test, self.batch_size) if self.test else None
        self.loader_full = DataLoader(self.dataset, self.batch_size)

        return self.loader_train, self.loader_validation, self.loader_test

    # -------- Reproducibility ---------------
    def new_split(self, valid_percent, test_percent=None, random_seed=None):
        """Creates train/validation or train/validation/test splits
            depending on provided parameters
            """
        self.split_info['random_seed'] = random_seed if random_seed else int(time.time())
        self.split_info.update(valid_percent=valid_percent, test_percent=test_percent)
        
        return self.load_split()

    def load_split(self, split_info=None, batch_size=None):
        """Get the split by provided parameters. Can be used to reproduce splits on the same dataset.
            NOTE this function re-initializes torch random number generator!
        """
        if split_info:
            if any([key not in split_info for key in self.split_info]):
                raise ValueError('Specified split information is not full: {}'.format(split_info))
            self.split_info = split_info

        torch.manual_seed(self.split_info['random_seed'])
        valid_size = (int)(len(self.dataset) * self.split_info['valid_percent'] / 100)
        if self.split_info['test_percent']:
            test_size = (int)(len(self.dataset) * self.split_info['test_percent'] / 100)
            self.training, self.validation, self.test = torch.utils.data.random_split(
                self.dataset, (len(self.dataset) - valid_size - test_size, valid_size, test_size))
        else:
            self.training, self.validation = torch.utils.data.random_split(
                self.dataset, (len(self.dataset) - valid_size, valid_size))
            self.test = None

        if batch_size is not None:
            self.batch_size = batch_size
        if self.batch_size is not None:
            self.new_loaders()  # s.t. loaders could be used right away

        print('{} split: {} / {} / {}'.format(self.dataset.name, len(self.training), 
                                              len(self.validation) if self.validation else None, 
                                              len(self.test) if self.test else None))

        return self.training, self.validation, self.test

    def save_to_wandb(self, experiment):
        """Save current data info to the wandb experiment"""
        # Split
        experiment.add_config('data_split', self.split_info)
        # data info
        self.dataset.save_to_wandb(experiment)

    # ---------- Standardinzation ----------------
    def standardize_data(self):
        """Apply data normalization based on stats from training set"""
        self.dataset.standardize(self.training)

    # --------- Managing predictions on this data ---------
    def predict(self, model, save_to, sections=['test'], single_batch=False):
        """Save model predictions on the given dataset section"""
        # Main path
        prediction_path = save_to / ('nn_pred_' + self.dataset.name + datetime.now().strftime('%y%m%d-%H-%M-%S'))
        prediction_path.mkdir(parents=True, exist_ok=True)

        for section in sections:
            # Section path
            section_dir = prediction_path / section
            section_dir.mkdir(parents=True, exist_ok=True)

            device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
            model.to(device)
            model.eval()
            with torch.no_grad():
                loader = self.get_loader(section)
                if loader:
                    if single_batch:
                        batch = next(iter(loader))    # might have some issues, see https://github.com/pytorch/pytorch/issues/1917
                        features = batch['features'].to(device)
                        self.dataset.save_prediction_batch(model(features), batch['name'], section_dir)
                    else:
                        for batch in loader:
                            features = batch['features'].to(device)
                            self.dataset.save_prediction_batch(model(features), batch['name'], section_dir)
        return prediction_path


# ------------------ Transforms ----------------
def _dict_to_tensors(dict_obj):  # helper
    """convert a dictionary with numeric values into a new dictionary with torch tensors"""
    new_dict = dict.fromkeys(dict_obj.keys())
    for key, value in dict_obj.items():
        if isinstance(value, np.ndarray):
            new_dict[key] = torch.from_numpy(value).float()
        else:
            new_dict[key] = torch.Tensor(value)
    return new_dict


# Custom transforms -- to tensor
class SampleToTensor(object):
    """Convert ndarrays in sample to Tensors."""
    
    def __call__(self, sample):
        features, gt = sample['features'], sample['ground_truth']

        if isinstance(gt, dict):
            new_gt = _dict_to_tensors(gt)
        else: 
            new_gt = torch.from_numpy(gt).float() if gt is not None else torch.Tensor(), 
        
        return {
            'features': torch.from_numpy(features).float() if features is not None else torch.Tensor(), 
            'ground_truth': new_gt, 
            'name': sample['name']
        }


class FeatureStandartization():
    """Normalize features of provided sample with given stats"""
    def __init__(self, shift, scale):
        self.shift = torch.Tensor(shift)
        self.scale = torch.Tensor(scale)
    
    def __call__(self, sample):
        return {
            'features': (sample['features'] - self.shift) / self.scale,
            'ground_truth': sample['ground_truth'],
            'name': sample['name']
        }


class GTtandartization():
    """Normalize features of provided sample with given stats
        * Supports multimodal gt represented as dictionary
        * For dictionary gts, only those values are updated for which the stats are provided
    """
    def __init__(self, shift, scale):
        """If ground truth is a dictionary in itself, the provided values should also be dictionaries"""
        
        self.shift = _dict_to_tensors(shift) if isinstance(shift, dict) else torch.Tensor(shift)
        self.scale = _dict_to_tensors(scale) if isinstance(scale, dict) else torch.Tensor(scale)
    
    def __call__(self, sample):
        gt = sample['ground_truth']
        if isinstance(gt, dict):
            new_gt = dict.fromkeys(gt.keys())
            for key, value in gt.items():
                new_gt[key] = value
                if key in self.shift:
                    new_gt[key] = new_gt[key] - self.shift[key]
                if key in self.scale:
                    new_gt[key] = new_gt[key] / self.scale[key]
                
                # if shift and scale are not set, the value is kept as it is
        else:
            new_gt = (gt - self.shift) / self.scale

        return {
            'features': sample['features'],
            'ground_truth': new_gt,
            'name': sample['name']
        }


# --------------------- Datasets -------------------------

class BaseDataset(Dataset):
    """Ensure that all my datasets follow this interface"""
    def __init__(self, root_dir, start_config={}, gt_caching=False, feature_caching=False, transforms=[]):
        """Kind of Universal init for my datasets
            if cashing is enabled, datapoints will stay stored in memory on first call to them: might speed up data processing by reducing file reads"""
        self.root_path = Path(root_dir)
        self.name = self.root_path.name
        self.config = {}
        self.update_config(start_config)
        
        # list of items = subfolders
        _, dirs, _ = next(os.walk(self.root_path))
        self.datapoints_names = dirs
        self._clean_datapoint_list()
        self.gt_cached = {}
        self.gt_caching = gt_caching
        if gt_caching:
            print('BaseDataset::Info::Storing datapoints ground_truth info in memory')
        self.feature_cached = {}
        self.feature_caching = feature_caching
        if feature_caching:
            print('BaseDataset::Info::Storing datapoints feature info in memory')

        # Use default tensor transform + the ones from input
        self.transforms = [SampleToTensor()] + transforms

        # statistics already there --> need to apply it
        if 'standardize' in self.config:
            self.standardize()

        # DEBUG -- access all the datapoints to pre-populate the cache of the data
        # self._renew_cache()

        # in\out sizes
        self._estimate_data_shape()

    def save_to_wandb(self, experiment):
        """Save data cofiguration to current expetiment run"""
        experiment.add_config('dataset', self.config)

    def update_transform(self, transform):
        """apply new transform when loading the data"""
        raise NotImplementedError('BaseDataset:Error:current transform support is poor')
        # self.transform = transform

    def __len__(self):
        """Number of entries in the dataset"""
        return len(self.datapoints_names)  

    def __getitem__(self, idx):
        """Called when indexing: read the corresponding data. 
        Does not support list indexing"""
        
        if torch.is_tensor(idx):  # allow indexing by tensors
            idx = idx.tolist()

        folder_elements = None  
        datapoint_name = self.datapoints_names[idx]

        if datapoint_name in self.gt_cached:  # might not be compatible with list indexing
            ground_truth = self.gt_cached[datapoint_name]
        else:
            folder_elements = [file.name for file in (self.root_path / datapoint_name).glob('*')]  # all files in this directory
            ground_truth = self._get_ground_truth(datapoint_name, folder_elements)
            if self.gt_caching:
                self.gt_cached[datapoint_name] = ground_truth
        
        if datapoint_name in self.feature_cached:
            features = self.feature_cached[datapoint_name]
        else:
            folder_elements = folder_elements if folder_elements is not None else [file.name for file in (self.root_path / datapoint_name).glob('*')]
            features = self._get_features(datapoint_name, folder_elements)
            
            if self.feature_caching:  # save read values 
                self.feature_cached[datapoint_name] = features
        
        sample = {'features': features, 'ground_truth': ground_truth, 'name': datapoint_name}

        # apply transfomations (equally to samples from files or from cache)
        for transform in self.transforms:
            sample = transform(sample)

        # if datapoint_name == 'tee_AME9SSCR7X':
        #     print(self.transforms)
        #     print('After transform: {}'.format(sample['features']))

        return sample

    def update_config(self, in_config):
        """Define dataset configuration:
            * to be part of experimental setup on wandb
            * Control obtainign values for datapoints"""
        self.config.update(in_config)
        if 'name' in self.config and self.name != self.config['name']:
            print('BaseDataset:Warning:dataset name ({}) in loaded config does not match current dataset name ({})'.format(self.config['name'], self.name))

        self.config['name'] = self.name
        self._update_on_config_change()

    def _renew_cache(self):
        """Flush the cache and re-fill it with updated information if any kind of caching is enabled"""
        self.gt_cached = {}
        self.feature_cached = {}
        if self.feature_caching or self.gt_caching:
            for i in range(len(self)):
                self[i]
            print('Data cached!')

    # -------- Data-specific functions --------
    def save_prediction_batch(self, predictions, datanames, save_to):
        """Saves predicted params of the datapoint to the original data folder"""
        pass

    def standardize(self, training=None):
        """Use element normalization/standardization based on stats from the training subset.
            Dataset is the object most aware of the datapoint structure hence it's the place to calculate & use the normalization.
            Uses either of two: 
            * training subset to calculate the data statistics -- the stats are only based on training subsection of the data
            * if stats info is already defined in config, it's used instead of calculating new statistics (usually when calling to restore dataset from existing experiment)
            configuration has a priority: if it's given, the statistics are NOT recalculated even if training set is provided:
                this allows to save some time
        """
        print('{}::Warning::No normalization is implemented'.format(self.__class__.__name__))

    def _clean_datapoint_list(self):
        """Remove non-datapoints subfolders, failing cases, etc. Children are to override this function when needed"""
        # See https://stackoverflow.com/questions/57042695/calling-super-init-gives-the-wrong-method-when-it-is-overridden
        pass

    def _get_features(self, datapoint_name, folder_elements=None):
        """Read/generate datapoint features"""
        return np.array([0])

    def _get_ground_truth(self, datapoint_name, folder_elements=None):
        """Ground thruth prediction for a datapoint"""
        return np.array([0])

    def _estimate_data_shape(self):
        """Get sizes/shapes of a datapoint for external references"""
        elem = self[0]
        feature_size = elem['features'].shape[0]
        gt_size = elem['ground_truth'].shape[0] if hasattr(elem['ground_truth'], 'shape') else None
        # sanity checks
        if ('feature_size' in self.config and feature_size != self.config['feature_size'] 
                or 'ground_truth_size' in self.config and gt_size != self.config['ground_truth_size']):
            raise RuntimeError('BaseDataset::Error::feature shape ({}) or ground truth shape ({}) from loaded config do not match calculated values: {}, {}'.format(
                self.config['feature_size'], self.config['ground_truth_size'], feature_size, gt_size))

        self.config['feature_size'], self.config['ground_truth_size'] = feature_size, gt_size

    def _update_on_config_change(self):
        """Update object inner state after config values have changed"""
        pass


class GarmentBaseDataset(BaseDataset):
    """Base class to work with data from custom garment datasets"""
        
    def __init__(self, root_dir, start_config={}, gt_caching=False, feature_caching=False, transforms=[]):
        """
        Args:
            root_dir (string): Directory with all examples as subfolders
            transform (callable, optional): Optional transform to be applied
                on a sample.
        """
        self.dataset_props = Properties(Path(root_dir) / 'dataset_properties.json')
        if not self.dataset_props['to_subfolders']:
            raise NotImplementedError('Working with datasets with all datapopints ')
        
        super().__init__(root_dir, start_config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
     
    def save_to_wandb(self, experiment):
        """Save data cofiguration to current expetiment run"""
        super().save_to_wandb(experiment)

        shutil.copy(self.root_path / 'dataset_properties.json', experiment.local_path())
    
    def save_prediction_batch(self, predictions, datanames, save_to):
        """Saves predicted params of the datapoint to the original data folder.
            Returns list of paths to files with prediction visualizations"""

        prediction_imgs = []
        for prediction, name in zip(predictions, datanames):

            pattern = self._pred_to_pattern(prediction, name)

            # save
            final_dir = pattern.serialize(save_to, to_subfolder=True, tag='_predicted_')
            final_file = pattern.name + '_predicted__pattern.png'
            prediction_imgs.append(Path(final_dir) / final_file)

            # copy originals for comparison
            for file in (self.root_path / name).glob('*'):
                if ('.png' in file.suffix) or ('.json' in file.suffix):
                    shutil.copy2(str(file), str(final_dir))
        return prediction_imgs

    # ------ Garment Data-specific basic functions --------
    def _clean_datapoint_list(self):
        """Remove all elements marked as failure from the datapoint list"""
        self.datapoints_names.remove('renders')  # TODO read ignore list from props

        fails_dict = self.dataset_props['sim']['stats']['fails']
        # TODO allow not to ignore some of the subsections
        for subsection in fails_dict:
            for fail in fails_dict[subsection]:
                try:
                    self.datapoints_names.remove(fail)
                    print('Dataset:: {} ignored'.format(fail))
                except ValueError:  # if fail was already removed based on previous failure subsection
                    pass

    def _pred_to_pattern(self, prediction, dataname):
        """Convert given predicted value to pattern object"""
        return None

    # ------------- Datapoints Utils --------------
    def _sample_points(self, datapoint_name, folder_elements):
        """Make a sample from the 3d surface from a given datapoint files"""
        obj_list = [file for file in folder_elements if 'sim.obj' in file]
        if not obj_list:
            raise RuntimeError('Dataset:Error: geometry file *sim.obj not found for {}'.format(datapoint_name))
        
        verts, faces = igl.read_triangle_mesh(str(self.root_path / datapoint_name / obj_list[0]))
        points = GarmentBaseDataset.sample_mesh_points(self.config['mesh_samples'], verts, faces)

        # Debug
        # if datapoint_name == 'skirt_4_panels_00HUVRGNCG':
        #     meshplot.offline()
        #     meshplot.plot(points, c=points[:, 0], shading={"point_size": 3.0})
        return points

    @staticmethod
    def sample_mesh_points(num_points, verts, faces):
        """A routine to sample requested number of points from a given mesh
            Returns points in world coordinates"""

        barycentric_samples, face_ids = igl.random_points_on_mesh(num_points, verts, faces)
        face_ids[face_ids >= len(faces)] = len(faces) - 1  # workaround for https://github.com/libigl/libigl/issues/1531

        # convert to world coordinates
        points = np.empty(barycentric_samples.shape)
        for i in range(len(face_ids)):
            face = faces[face_ids[i]]
            barycentric_coords = barycentric_samples[i]
            face_verts = verts[face]
            points[i] = np.dot(barycentric_coords, face_verts)

        return points

    def _read_pattern(self, datapoint_name, folder_elements, with_placement=False, with_stitches=False):
        """Read given pattern in tensor representation from file"""
        spec_list = [file for file in folder_elements if 'specification.json' in file]
        if not spec_list:
            raise RuntimeError('GarmentBaseDataset::Error::*specification.json not found for {}'.format(datapoint_name))
        
        pattern = BasicPattern(self.root_path / datapoint_name / spec_list[0])
        return pattern.pattern_as_tensors(with_placement=with_placement, with_stitches=with_stitches)

    def _pattern_from_tenzor(self, dataname, 
                             tenzor, 
                             rotations=None, translations=None, stitches=None,
                             std_config={}, supress_error=True):
        """Shortcut to create a pattern object from given tenzor and suppress exceptions if those arize"""
        if std_config and 'standardize' in std_config:
            tenzor = tenzor * self.config['standardize']['scale'] + self.config['standardize']['shift']

        pattern = VisPattern(view_ids=False)
        pattern.name = dataname
        try: 
            pattern.pattern_from_tensors(
                tenzor, panel_rotations=rotations, panel_translations=translations, stitches=stitches,
                padded=True)   
        except RuntimeError as e:
            if not supress_error:
                raise e
            print('Garment3DPatternDataset::Warning::{}: {}'.format(dataname, e))
            pass

        return pattern

    # -------- Generalized Utils
    def _unpad(self, element, tolerance=1.e-5):
        """Return copy of input element without padding from given element. Used to unpad edge sequences in pattern-oriented datasets"""
        # NOTE: might be some false removal of zero edges in the middle of the list.
        if torch.is_tensor(element):        
            bool_matrix = torch.isclose(element, torch.zeros_like(element), atol=tolerance)  # per-element comparison with zero
            selection = ~torch.all(bool_matrix, axis=1)  # only non-zero rows
        else:  # numpy
            selection = ~np.all(np.isclose(element, 0, atol=tolerance), axis=1)  # only non-zero rows
        return element[selection]

    def _get_distribution_stats(self, input_batch, padded=False):
        """Calculates mean & std values for the input tenzor along the last dimention"""

        input_batch = input_batch.view(-1, input_batch.shape[-1])
        if padded:
            input_batch = self._unpad(input_batch)  # remove rows with zeros

        # per dimention means
        mean = input_batch.mean(axis=0)
        # per dimention stds
        stds = ((input_batch - mean) ** 2).sum(0)
        stds = torch.sqrt(stds / input_batch.shape[0])

        return mean, stds

    def _get_norm_stats(self, input_batch, padded=False):
        """Calculate shift & scaling values needed to normalize input tenzor 
            along the last dimention to [0, 1] range"""
        input_batch = input_batch.view(-1, input_batch.shape[-1])
        if padded:
            input_batch = self._unpad(input_batch)  # remove rows with zeros

        # per dimention info
        min_vector, _ = torch.min(input_batch, 0)
        max_vector, _ = torch.max(input_batch, 0)
        scale = torch.empty_like(min_vector)
        # avoid division by zero
        for idx, (tmin, tmax) in enumerate(zip(min_vector, max_vector)): 
            if torch.isclose(tmin, tmax):
                scale[idx] = tmin if not torch.isclose(tmin, torch.zeros(1)) else 1.
            else:
                scale[idx] = tmax - tmin
        
        return min_vector, scale


class GarmentParamsDataset(GarmentBaseDataset):
    """
    For loading the custom generated data:
        * features: Coordinates of 3D mesh sample points flattened into vector
        * Ground_truth: parameters used to generate a garment
    """
    
    def __init__(self, root_dir, start_config={'mesh_samples': 1000}, gt_caching=False, feature_caching=False, transforms=[]):
        """
        Args:
            root_dir (string): Directory with all examples as subfolders
            transform (callable, optional): Optional transform to be applied
                on a sample.
        """
        if 'mesh_samples' not in start_config:  
            start_config['mesh_samples'] = 1000  # some default to ensure it's set

        super().__init__(root_dir, start_config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
    
    # ------ Data-specific basic functions --------
    def _get_features(self, datapoint_name, folder_elements):
        """Get mesh vertices for given datapoint with given file list of datapoint subfolder"""
        points = self._sample_points(datapoint_name, folder_elements)

        return points.ravel()  # flat vector as a feature
        
    def _get_ground_truth(self, datapoint_name, folder_elements):
        """Pattern parameters from a given datapoint subfolder"""
        spec_list = [file for file in folder_elements if 'specification.json' in file]
        if not spec_list:
            raise RuntimeError('Dataset:Error: *specification.json not found for {}'.format(datapoint_name))
        
        pattern = ParametrizedPattern(self.root_path / datapoint_name / spec_list[0])

        return np.array(pattern.param_values_list())
   
    def _pred_to_pattern(self, prediction, dataname):
        """Convert given predicted value to pattern object"""
        prediction = prediction.tolist()
        pattern = VisPattern(str(self.root_path / dataname / 'specification.json'), view_ids=False)  # with correct pattern name

        # apply new parameters
        pattern.apply_param_list(prediction)

        return pattern


class Garment3DParamsDataset(GarmentParamsDataset):
    """For loading the custom generated data:
        * features: list of 3D coordinates of 3D mesh sample points (2D matrix)
        * Ground_truth: parameters used to generate a garment
    """
    def __init__(self, root_dir, start_config={'mesh_samples': 1000}, gt_caching=False, feature_caching=False, transforms=[]):
        super().__init__(root_dir, start_config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
    
    # the only difference with parent class in the shape of the features
    def _get_features(self, datapoint_name, folder_elements):
        points = self._sample_points(datapoint_name, folder_elements)
        return points  # return in 3D


class GarmentPanelDataset(GarmentBaseDataset):
    """Experimental loading of custom generated data to be used in AE:
        * features: a panel edges represented as a sequence. Panel is chosen randomly. 
        * ground_truth is not used
        * When saving predictions, the predicted panel is always saved as panel with name provided in config 

    """
    def __init__(self, root_dir, start_config={}, gt_caching=False, feature_caching=False, transforms=[]):
        config = {'panel_name': 'front'}
        config.update(start_config)
        super().__init__(root_dir, config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
        self.config['element_size'] = self[0]['features'].shape[1]
    
    def standardize(self, training=None):
        """Use shift and scaling for normalization of output features & restoring input predictions.
            Accepts either of two inputs: 
            * training subset to calculate the data statistics -- the stats are only based on training subsection of the data
            * if stats info is given, it's used instead of calculating new statistics (usually when calling to restore dataset from existing experiment)
            configuration has a priority: if it's given, the statistics are NOT recalculated even if training set is provided
        """
        print('GarmentPanelDataset::Using data normalization')

        if 'standardize' in self.config:
            print('{}::Using stats from config'.format(self.__class__.__name__))
            stats = self.config['standardize']
        elif training is not None:
            loader = DataLoader(training, batch_size=len(training), shuffle=False)
            for batch in loader:
                feature_mean, feature_stds = self._get_distribution_stats(batch['features'], padded=True)
                # NOTE mean values for panels are zero due to loop property 
                # panel components CANNOT be shifted to keep the loop property intact 
                # hence enforce zeros in mean value for edge coordinates
                feature_mean[0] = feature_mean[1] = 0

                break  # only one batch out there anyway
            self.config['standardize'] = {'shift': feature_mean.cpu().numpy(), 'scale': feature_stds.cpu().numpy()}
            stats = self.config['standardize']
        else:  # no info provided
            raise ValueError('GarmentPanelDataset::Error::Standardization cannot be applied: supply either stats or training set to use standardization')

        # clean-up tranform list to avoid duplicates
        self.transforms = [transform for transform in self.transforms if not isinstance(transform, FeatureStandartization)]

        self.transforms.append(FeatureStandartization(stats['shift'], stats['scale']))

    def _get_features(self, datapoint_name, folder_elements):
        """Get mesh vertices for given datapoint with given file list of datapoint subfolder"""
        # sequence = pattern.panel_as_sequence(self.config['panel_name'])
        pattern_nn = self._read_pattern(datapoint_name, folder_elements)
        # return random panel from a pattern
        return pattern_nn[torch.randint(pattern_nn.shape[0], (1,))]
        
    def _get_ground_truth(self, datapoint_name, folder_elements):
        """The dataset targets AutoEncoding tasks -- no need for features"""
        return None

    def _pred_to_pattern(self, prediction, dataname):
        """Save predicted value for a panel to pattern object"""
        # Not using standard util for saving as One panel out of all need update

        prediction = prediction.cpu().numpy()

        if 'standardize' in self.config:
            prediction = prediction * self.config['standardize']['scale'] + self.config['standardize']['shift']

        pattern = VisPattern(str(self.root_path / dataname / 'specification.json'), view_ids=False)  # with correct pattern name

        # apply new edge info
        try: 
            pattern.panel_from_numeric(self.config['panel_name'], self._unpad(prediction, 1.5))   # we can set quite high tolerance! Normal edges are quite long
        except RuntimeError as e:
            print('GarmentPanelDataset::Warning::{}: {}'.format(dataname, e))
            pass

        return pattern


class Garment2DPatternDataset(GarmentPanelDataset):
    """Dataset definition for 2D pattern autoencoder
        * features: a 'front' panel edges represented as a sequence
        * ground_truth is not used as in Panel dataset"""
    def __init__(self, root_dir, start_config={}, gt_caching=False, feature_caching=False, transforms=[]):
        super().__init__(root_dir, start_config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
        self.config['panel_len'] = self[0]['features'].shape[1]
        self.config['element_size'] = self[0]['features'].shape[2]
    
    def _get_features(self, datapoint_name, folder_elements):
        """Get mesh vertices for given datapoint with given file list of datapoint subfolder"""
        return self._read_pattern(datapoint_name, folder_elements)
        
    def _pred_to_pattern(self, prediction, dataname):
        """Convert given predicted value to pattern object"""
        prediction = prediction.cpu().numpy()
        return self._pattern_from_tenzor(dataname, prediction, std_config=self.config, supress_error=True)


class Garment3DPatternDataset(GarmentBaseDataset):
    """Dataset definition for extracting pattern from 3D garment shape:
        * features: point samples from 3D surface
        * ground truth: tensor representation of corresponding pattern"""
    
    def __init__(self, root_dir, start_config={}, gt_caching=False, feature_caching=False, transforms=[]):
        if 'mesh_samples' not in start_config:
            start_config['mesh_samples'] = 2000  # default value if not given -- a bettern gurantee than a default value in func params
        super().__init__(root_dir, start_config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
        self.config['panel_len'] = self[0]['ground_truth'].shape[1]
        self.config['element_size'] = self[0]['ground_truth'].shape[2]
    
    def standardize(self, training=None):
        """Use mean&std for standardization of output edge features & restoring input predictions.
            Accepts either of two inputs: 
            * training subset to calculate the data statistics -- the stats are only based on training subsection of the data
            * if stats info is already defined in config, it's used instead of calculating new statistics (usually when calling to restore dataset from existing experiment)
            configuration has a priority: if it's given, the statistics are NOT recalculated even if training set is provided
                => speed-up by providing stats or speeding up multiple calls to this function
        """
        print('Garment3DPatternDataset::Using data normalization for features & ground truth')

        if 'standardize' in self.config:
            print('{}::Using stats from config'.format(self.__class__.__name__))
            stats = self.config['standardize']
        elif training is not None:
            loader = DataLoader(training, batch_size=len(training), shuffle=False)
            for batch in loader:
                feature_mean, feature_stds = self._get_distribution_stats(batch['features'], padded=False)

                gt_mean, gt_stds = self._get_distribution_stats(batch['ground_truth'], padded=True)
                # NOTE mean values for panels are zero due to loop property 
                # panel components CANNOT be shifted to keep the loop property intact 
                # hence enforce zeros in mean value for edge coordinates
                gt_mean[0] = gt_mean[1] = 0
                
                break  # only one batch out there anyway

            self.config['standardize'] = {
                'shift': gt_mean.cpu().numpy(), 'scale': gt_stds.cpu().numpy(), 
                'f_shift': feature_mean.cpu().numpy(), 'f_scale': feature_stds.cpu().numpy()}
            stats = self.config['standardize']
        else:  # nothing is provided
            raise ValueError('Garment3DPatternDataset::Error::Standardization cannot be applied: supply either stats in config or training set to use standardization')

        # clean-up tranform list to avoid duplicates
        self.transforms = [transform for transform in self.transforms if not isinstance(transform, GTtandartization) and not isinstance(transform, FeatureStandartization)]

        self.transforms.append(GTtandartization(stats['shift'], stats['scale']))
        self.transforms.append(FeatureStandartization(stats['f_shift'], stats['f_scale']))

    def _get_features(self, datapoint_name, folder_elements):
        """Get mesh vertices for given datapoint with given file list of datapoint subfolder"""
        points = self._sample_points(datapoint_name, folder_elements)
        return points  # return in 3D
        
    def _get_ground_truth(self, datapoint_name, folder_elements):
        """Get the pattern representation"""
        return self._read_pattern(datapoint_name, folder_elements)

    def _pred_to_pattern(self, prediction, dataname):
        """Convert given predicted value to pattern object"""
        prediction = prediction.cpu().numpy()
        return self._pattern_from_tenzor(dataname, prediction, std_config=self.config, supress_error=True)


class Garment3DPatternFullDataset(GarmentBaseDataset):
    """Dataset with full pattern definition as ground truth
        * it includes not only every panel outline geometry, but also 3D placement and stitches information
        Defines 3D samples from the point cloud as features
    """
    def __init__(self, root_dir, start_config={}, gt_caching=False, feature_caching=False, transforms=[]):
        if 'mesh_samples' not in start_config:
            start_config['mesh_samples'] = 2000  # default value if not given -- a bettern gurantee than a default value in func params
        super().__init__(root_dir, start_config, 
                         gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
        self.config['pattern_len'] = self[0]['ground_truth']['outlines'].shape[0]
        self.config['panel_len'] = self[0]['ground_truth']['outlines'].shape[1]
        self.config['element_size'] = self[0]['ground_truth']['outlines'].shape[2]
        self.config['rotation_size'] = self[0]['ground_truth']['rotations'].shape[1]
        self.config['translation_size'] = self[0]['ground_truth']['translations'].shape[1]
    
    def standardize(self, training=None):
        """Use shifting and scaling for fitting data to interval comfortable for NN training.
            Accepts either of two inputs: 
            * training subset to calculate the data statistics -- the stats are only based on training subsection of the data
            * if stats info is already defined in config, it's used instead of calculating new statistics (usually when calling to restore dataset from existing experiment)
            configuration has a priority: if it's given, the statistics are NOT recalculated even if training set is provided
                => speed-up by providing stats or speeding up multiple calls to this function
        """
        print('Garment3DPatternFullDataset::Using data normalization for features & ground truth')

        if 'standardize' in self.config:
            print('{}::Using stats from config'.format(self.__class__.__name__))
            stats = self.config['standardize']
        elif training is not None:
            loader = DataLoader(training, batch_size=len(training), shuffle=False)
            for batch in loader:
                feature_shift, feature_scale = self._get_distribution_stats(batch['features'], padded=False)

                gt = batch['ground_truth']
                panel_shift, panel_scale = self._get_distribution_stats(gt['outlines'], padded=True)
                # NOTE mean values for panels are zero due to loop property 
                # panel components SHOULD NOT be shifted to keep the loop property intact 
                panel_shift[0] = panel_shift[1] = 0

                # Use min\scale (normalization) instead of Gaussian stats for translation
                # No padding as zero translation is a valid value
                transl_min, transl_scale = self._get_norm_stats(gt['translations'])
                rot_min, rot_scale = self._get_norm_stats(gt['rotations'])

                break  # only one batch out there anyway

            self.config['standardize'] = {
                'f_shift': feature_shift.cpu().numpy(), 
                'f_scale': feature_scale.cpu().numpy(),
                'gt_shift': {
                    'outlines': panel_shift.cpu().numpy(), 
                    # 'rotations': np.zeros(self.config['rotation_size']),  # not applying std to rotation
                    'rotations': rot_min.cpu().numpy(),
                    'translations': transl_min.cpu().numpy(), 
                },
                'gt_scale': {
                    'outlines': panel_scale.cpu().numpy(), 
                    # 'rotations': np.ones(self.config['rotation_size']),  # not applying std to rotation
                    'rotations': rot_scale.cpu().numpy(),
                    'translations': transl_scale.cpu().numpy(),
                }
            }
            stats = self.config['standardize']
        else:  # nothing is provided
            raise ValueError('Garment3DPatternFullDataset::Error::Standardization cannot be applied: supply either stats in config or training set to use standardization')

        # clean-up tranform list to avoid duplicates
        self.transforms = [transform for transform in self.transforms if not isinstance(transform, GTtandartization) and not isinstance(transform, FeatureStandartization)]

        self.transforms.append(GTtandartization(stats['gt_shift'], stats['gt_scale']))
        self.transforms.append(FeatureStandartization(stats['f_shift'], stats['f_scale']))

    def save_prediction_batch(self, predictions, datanames, save_to):
        """Saves predicted params of the datapoint to the original data folder.
            Returns list of paths to files with prediction visualizations
            Assumes that the number of predictions matches the number of provided data names"""

        prediction_imgs = []
        for idx, name in enumerate(datanames):

            # "unbatch" dictionary
            prediction = {
                'outlines': predictions['outlines'][idx], 
                'rotations': predictions['rotations'][idx], 
                'translations': predictions['translations'][idx],
                'stitch_tags': predictions['stitch_tags'][idx]
            }

            pattern = self._pred_to_pattern(prediction, name)

            # save
            final_dir = pattern.serialize(save_to, to_subfolder=True, tag='_predicted_')
            final_file = pattern.name + '_predicted__pattern.png'
            prediction_imgs.append(Path(final_dir) / final_file)

            # copy originals for comparison
            for file in (self.root_path / name).glob('*'):
                if ('.png' in file.suffix) or ('.json' in file.suffix):
                    shutil.copy2(str(file), str(final_dir))
        return prediction_imgs

    @staticmethod
    def tags_to_stitches(stitch_tags):
        """
        Convert per-edge per panel stitch tags into the list of connected edge pairs
        """
        print(stitch_tags)

        zero_tag = torch.zeros(stitch_tags.shape[-1]).to(stitch_tags.device)
        print(zero_tag)

        non_zero_tags = []
        for panel_id in range(stitch_tags.shape[0]):
            for edge_id in range(stitch_tags.shape[1]):
                if not all(torch.isclose(stitch_tags[panel_id][edge_id], zero_tag, 0.01)):
                    non_zero_tags.append((panel_id, edge_id))
        
        print(len(non_zero_tags), non_zero_tags)
        
        # NOTE this part could be implemented smarter
        # but the total number of edges is never too large, so I decided to go with naive O(n^2) solution
        stitches = []
        for tag1_id in range(len(non_zero_tags)):
            edge_1 = non_zero_tags[tag1_id]
            tag1 = stitch_tags[edge_1[0]][edge_1[1]]
            for tag2_id in range(tag1_id + 1, len(non_zero_tags)):
                edge_2 = non_zero_tags[tag2_id]
                if all(torch.isclose(tag1, stitch_tags[edge_2[0]][edge_2[1]], 0.1)):  # stitch found!
                    stitches.append([edge_1, edge_2])
        return stitches

    def _get_features(self, datapoint_name, folder_elements):
        """Get mesh vertices for given datapoint with given file list of datapoint subfolder"""
        points = self._sample_points(datapoint_name, folder_elements)
        return points  # return in 3D
      
    def _get_ground_truth(self, datapoint_name, folder_elements):
        """Get the pattern representation with 3D placement"""
        pattern, rots, tranls, stitches = self._read_pattern(
            datapoint_name, folder_elements, with_placement=True, with_stitches=True)
        return {'outlines': pattern, 'rotations': rots, 'translations': tranls, 'stitches': stitches}

    def _pred_to_pattern(self, prediction, dataname):
        """Convert given predicted value to pattern object"""

        pattern, rots, transls = prediction['outlines'], prediction['rotations'], prediction['translations']
        stitch_tags = prediction['stitch_tags']

        # undo standardization  (outside of generinc conversion function due to custom std structure)
        gt_shifts = self.config['standardize']['gt_shift']
        gt_scales = self.config['standardize']['gt_scale']
        pattern = pattern.cpu().numpy() * gt_scales['outlines'] + gt_shifts['outlines']
        rots = rots.cpu().numpy() * gt_scales['rotations'] + gt_shifts['rotations']
        transls = transls.cpu().numpy() * gt_scales['translations'] + gt_shifts['translations']

        # stitch tags to stitch list
        stitches = self.tags_to_stitches(stitch_tags)

        return self._pattern_from_tenzor(dataname, pattern, rots, transls, stitches, 
                                         std_config={}, supress_error=True)


class ParametrizedShirtDataSet(BaseDataset):
    """
    For loading the data of "Learning Shared Shape Space.." paper
    """
    
    def __init__(self, root_dir, start_config={'num_verts': 'all'}, gt_caching=False, feature_caching=False, transforms=[]):
        """
        Args:
            root_dir (string): Directory with all the t-shirt examples as subfolders
            transform (callable, optional): Optional transform to be applied
                on a sample.
        """
        # datapoint folder structure
        self.mesh_filename = 'shirt_mesh_r.obj'
        self.pattern_params_filename = 'shirt_info.txt'
        self.features_filename = 'visfea.mat'
        self.garment_3d_filename = 'shirt_mesh_r_tmp.obj'

        super().__init__(root_dir, start_config, gt_caching=gt_caching, feature_caching=feature_caching, transforms=transforms)
        
    def save_prediction_batch(self, predictions, datanames, save_to):
        """Saves predicted params of the datapoint to the original data folder.
            Returns list of paths to files with predictions"""
        
        prediction_files = []
        for prediction, name in zip(predictions, datanames):
            path_to_prediction = Path(save_to) / name
            path_to_prediction.mkdir(parents=True, exist_ok=True)
            
            prediction = prediction.tolist()
            with open(path_to_prediction / self.pattern_params_filename, 'w+') as f:
                f.writelines(['0\n', '0\n', ' '.join(map(str, prediction))])
                print('Saved ' + name)
            prediction_files.append(str(path_to_prediction / self.pattern_params_filename))
        return prediction_files

    # ------ Data-specific basic functions  -------
    def _clean_datapoint_list(self):
        """Remove non-datapoints subfolders, failing cases, etc. Children are to override this function when needed"""
        self.datapoints_names.remove('P3ORMPBNJJAJ')
    
    def _get_features(self, datapoint_name, folder_elements=None):
        """features parameters from a given datapoint subfolder"""
        assert (self.root_path / datapoint_name / self.garment_3d_filename).exists(), datapoint_name
        
        verts, _ = igl.read_triangle_mesh(str(self.root_path / datapoint_name / self.garment_3d_filename))
        
        if self.config['num_verts'] == 'all':
            return verts.ravel()
        
        return verts[:self.config['num_verts']].ravel()
        
    def _get_ground_truth(self, datapoint_name, folder_elements=None):
        """9 pattern size parameters from a given datapoint subfolder"""
        assert (self.root_path / datapoint_name / self.pattern_params_filename).exists(), datapoint_name
        
        # assuming that we need the numbers from the last line in file
        with open(self.root_path / datapoint_name / self.pattern_params_filename) as f:
            lines = f.readlines()
            params = np.fromstring(lines[-1], sep=' ')
        return params
       

if __name__ == "__main__":

    # data_location = r'D:\Data\CLOTHING\Learning Shared Shape Space_shirt_dataset_rest'
    system = Properties('./system.json')
    # dataset_folder = 'data_1000_skirt_4_panels_200616-14-14-40'
    # dataset_folder = 'data_1000_tee_200527-14-50-42_regen_200612-16-56-43'

    # data_location = Path(system['datasets_path']) / dataset_folder

    # dataset = Garment3DPatternFullDataset(data_location)

    # print(len(dataset), dataset.config)
    # print(dataset[0]['name'], dataset[0]['features'].shape)  # , dataset[0]['ground_truth'])

    # print(dataset[5]['ground_truth'])

    # datawrapper = DatasetWrapper(dataset)
    # datawrapper.new_split(10, 10, 300)

    # datawrapper.standardize_data()

    # print(dataset.config['standardize'])

    # print(dataset[0]['ground_truth'])
    # print(dataset[5]['features'])

    stitch_tags = torch.Tensor([[[-4.7732e-01,  5.2743e-01,  5.7846e-02],
         [-1.0317e+00,  9.7064e-01,  3.0478e-01],
         [ 4.0752e-01,  5.7923e-01,  2.0723e-03],
         [-1.0661e-02,  1.9779e-03, -3.6985e-03],
         [-6.2923e-03,  1.2806e-03,  4.1377e-03],
         [ 5.0416e-04,  2.9007e-03,  3.6366e-03],
         [ 6.6723e-04,  4.3958e-03,  4.5902e-03],
         [ 1.0667e-03,  4.9522e-03,  4.9037e-03],
         [ 1.6380e-03,  4.2972e-03,  7.1949e-03]],

        [[ 8.7757e-02,  6.4274e-01,  4.6296e-01],
         [-2.8895e-02,  5.9110e-01,  6.1186e-01],
         [-3.5284e-01,  3.8004e-01,  2.0254e-02],
         [ 1.1227e-01,  1.7332e-01, -5.7697e-04],
         [-2.7311e-04,  3.6193e-03,  1.3721e-03],
         [-3.5853e-03,  8.0185e-03,  4.7167e-03],
         [ 1.1996e-03,  8.8283e-03,  6.0051e-03],
         [ 2.7664e-03,  7.6386e-03,  5.8009e-03],
         [ 4.3352e-03,  6.9330e-03,  6.5485e-03]],

        [[ 7.5046e-01,  2.4409e-01,  7.9030e-01],
         [-1.0306e+00,  9.8453e-01,  3.0250e-01],
         [-3.3723e-01, -2.8226e-01, -5.8897e-01],
         [ 1.8906e-02, -1.3832e-02,  5.5172e-03],
         [-9.8226e-01, -2.1431e-02, -1.9767e-01],
         [ 2.9255e-01, -7.0595e-01,  1.2018e-01],
         [-5.2501e-01, -4.5708e-01,  3.0568e-01],
         [ 1.7425e-03, -2.6523e-03, -3.2320e-03],
         [ 3.4501e-03,  2.4877e-03,  1.5023e-04]],

        [[ 3.5013e-03, -2.4229e-04,  1.7040e-02],
         [-5.2547e-01, -4.3172e-01,  3.3254e-01],
         [ 7.4213e-01, -1.2066e+00, -3.2575e-01],  # Here
         [-9.6715e-01, -1.5716e-02, -1.8207e-01],
         [ 8.1369e-03, -9.7913e-03, -4.5263e-03],
         [ 4.6673e-03, -1.0336e-02, -3.8493e-03],
         [-3.3719e-01, -2.7093e-01, -5.9384e-01],
         [ 3.0050e-02,  6.0369e-01,  5.8586e-01],
         [ 7.6712e-01,  2.4150e-01,  7.9097e-01]],

        [[ 5.8924e-01, -3.3213e-01,  3.2885e-01],
         [ 1.6048e-01, -8.3018e-01,  6.7562e-02],
         [ 4.2251e-01, -3.7204e-01, -4.2598e-01],
         [ 3.1887e-02, -2.1434e-02,  3.5033e-02],
         [ 9.4733e-03,  3.4869e-03,  2.3111e-03],
         [ 3.9466e-03,  1.5404e-03, -1.0325e-03],
         [ 5.3823e-03,  2.9728e-03, -1.0817e-04],
         [ 4.3724e-03,  2.2851e-03,  1.7486e-04],
         [ 2.9140e-03,  3.8158e-03, -2.0858e-04]],

        [[ 3.3217e-01, -5.3243e-01, -2.8784e-01],
         [ 7.3228e-01, -1.2096e+00, -3.4954e-01], # Here
         [ 4.3159e-01, -2.4361e-01,  2.4041e-01],
         [ 1.3506e-02, -1.6694e-03,  6.6847e-03],
         [ 7.6342e-03,  2.6294e-03,  3.8481e-03],
         [ 5.8650e-03,  2.0788e-03, -2.5582e-03],
         [ 4.0614e-03,  4.7357e-03, -4.6884e-04],
         [ 4.8211e-03,  8.9094e-04, -1.8303e-03],
         [ 4.0147e-03,  2.1679e-03, -1.1023e-03]]])

    print(Garment3DPatternFullDataset.tags_to_stitches(stitch_tags))