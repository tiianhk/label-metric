import logging

from torch.utils.data import DataLoader
import lightning as L

from label_metric.datasets import BasicOrchideaSOL, TripletOrchideaSOL
from label_metric.samplers import SampleTripletsFromTree, WeightManager

class OrchideaSOLDataModule(L.LightningDataModule):
    
    def __init__(self,
        dataset_dir: str,
        min_num_per_leaf: int,
        duration: float,
        train_ratio: float,
        valid_ratio: float,
        logger: logging.Logger,
        dataset_sr: int,
        dataset_channel_num: int,
        more_level: int,
        weight_manager: WeightManager,
        batch_size: int, 
        num_workers: int
    ):
        super().__init__()
        self.dataset_dir = dataset_dir
        self.min_num_per_leaf = min_num_per_leaf
        self.duration = duration
        self.train_ratio = train_ratio
        self.valid_ratio = valid_ratio
        self.logger = logger
        self.dataset_sr = dataset_sr
        self.dataset_channel_num = dataset_channel_num
        self.more_level = more_level
        self.weight_manager = weight_manager
        self.batch_size = batch_size
        self.num_workers = num_workers

    def setup(self, stage: str) -> None:
        if stage == 'fit':
            self.train_set = TripletOrchideaSOL(
                dataset_dir = self.dataset_dir,
                split = 'train',
                min_num_per_leaf = self.min_num_per_leaf,
                duration = self.duration,
                train_ratio = self.train_ratio,
                valid_ratio = self.valid_ratio,
                logger = self.logger,
                dataset_sr = self.dataset_sr,
                dataset_channel_num = self.dataset_channel_num
            )
            self.triplet_sampler = SampleTripletsFromTree(
                dataset = self.train_set, 
                more_level = self.more_level, 
                logger = self.logger,
                weight_manager = self.weight_manager
            )
            self.valid_set = BasicOrchideaSOL(
                dataset_dir = self.dataset_dir,
                split = 'valid',
                min_num_per_leaf = self.min_num_per_leaf,
                duration = self.duration,
                train_ratio = self.train_ratio,
                valid_ratio = self.valid_ratio,
                logger = self.logger,
                dataset_sr = self.dataset_sr,
                dataset_channel_num = self.dataset_channel_num
            )
        if stage == 'test':
            self.test_set = BasicOrchideaSOL(
                dataset_dir = self.dataset_dir,
                split = 'test',
                min_num_per_leaf = self.min_num_per_leaf,
                duration = self.duration,
                train_ratio = self.train_ratio,
                valid_ratio = self.valid_ratio,
                logger = self.logger,
                dataset_sr = self.dataset_sr,
                dataset_channel_num = self.dataset_channel_num
            )

    def train_dataloader(self):
        return DataLoader(
            self.train_set,
            sampler = self.triplet_sampler,
            batch_size = self.batch_size,
            num_workers = self.num_workers,
            drop_last = True
        )

    def val_dataloader(self):
        return DataLoader(
            self.valid_set,
            batch_size = self.batch_size,
            num_workers = self.num_workers,
            shuffle = False,
            drop_last = False
        )

    def test_dataloader(self):
        return DataLoader(
            self.test_set,
            batch_size = self.batch_size,
            num_workers = self.num_workers,
            shuffle = False,
            drop_last = False
        )

if __name__ == '__main__':

    # example code

    import lightning as L
    L.seed_everything(2024)
    from label_metric.utils.log_utils import setup_logger
    logger = logging.getLogger(__name__)
    setup_logger(logger)

    weight_manager = WeightManager(logger, active = True)

    dm = OrchideaSOLDataModule(
        dataset_dir = '/data/scratch/acw751/_OrchideaSOL2020_release',
        min_num_per_leaf = 10,
        duration = 1.0,
        train_ratio = 0.8,
        valid_ratio = 0.1,
        logger = logger,
        dataset_sr = 44100,
        dataset_channel_num = 1,
        more_level = 1,
        weight_manager = weight_manager,
        batch_size = 32,
        num_workers = 2
    )

    dm.setup('fit')
    train_loader = dm.train_dataloader()
    valid_loader = dm.val_dataloader()
    dm.setup('test')
    test_loader = dm.test_dataloader()
