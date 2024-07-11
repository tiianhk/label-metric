import logging

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import lightning as L
from torchmetrics import Accuracy
from torchmetrics.retrieval import RetrievalPrecision
from torchmetrics.functional.retrieval import retrieval_precision

from label_metric.samplers import WeightManager
from label_metric.losses import TripletLoss

class LabelMetricModule(L.LightningModule):

    def __init__(
        self,
        backbone_model: nn.Module,
        prediction_head: nn.Module,
        triplet_loss_fn: TripletLoss,
        lambda_weight: float,
        my_logger: logging.Logger,
        weight_manager: WeightManager,
        learning_rate: float,
        classification_accuracy_top_k: int,
        retrieval_precision_top_k: int
    ):
        super().__init__()
        self.backbone_model = backbone_model
        self.prediction_head = prediction_head
        self.triplet_loss_fn = triplet_loss_fn
        assert 0 <= lambda_weight <= 1
        self.lambda_weight = torch.tensor(lambda_weight)
        self.my_logger = my_logger
        self.weight_manager = weight_manager
        self.learning_rate = learning_rate
        self.ca_top_k = classification_accuracy_top_k
        self.rp_top_k = retrieval_precision_top_k
        self.accuracy = Accuracy(
            task = 'multiclass', 
            num_classes = prediction_head.num_classes
        )
        self.accuracy_top_k = Accuracy(
            task = 'multiclass', 
            num_classes = prediction_head.num_classes,
            top_k = self.ca_top_k
        )
        self.retrieval_precision = RetrievalPrecision(top_k=self.rp_top_k)

    def forward(self, x: torch.Tensor):
        embeddings = self.backbone_model(x)
        return embeddings

    def on_train_epoch_start(self):
        weights = self.weight_manager.get_weights()
        if weights is None:
            self.w_a = None
            self.w_p = None
            self.w_n = None
        else:
            self.w_a = weights['anc'].to(self.device)
            self.w_p = weights['pos'].to(self.device)
            self.w_n = weights['neg'].to(self.device)

    def training_step(self, batch, batch_idx):
        epoch_idx = self.current_epoch
        # get anchors, positives, negatives
        x_a, y_a = batch['anc']
        x_p, y_p = batch['pos']
        x_n, y_n = batch['neg']
        # embeddings
        z_a = self(x_a)
        z_p = self(x_p)
        z_n = self(x_n)
        # triplet loss
        triplet_loss = self.triplet_loss_fn(z_a, z_p, z_n) * self.lambda_weight
        # classification loss
        logits_a = self.prediction_head(z_a)
        assert hasattr(self, 'w_a'), 'anchor class weights have not been set yet'
        classification_loss = F.cross_entropy(logits_a, y_a, self.w_a) \
            * (1 - self.lambda_weight)
        # add
        loss = triplet_loss + classification_loss
        self.log('train_loss/triplet', triplet_loss)
        self.log('classification_loss/train', classification_loss)
        self.log('train_loss/classification', classification_loss)
        self.log('train_loss/total', loss)
        # self.my_logger.info(f'training epoch {epoch_idx} batch {batch_idx} '
        #                     f'triplet loss: {triplet_loss} '
        #                     f'classification loss: {classification_loss}')
        return loss

    def on_validation_epoch_start(self):
        self.val_embeddings = []
        self.val_labels = []

    def validation_step(self, batch, batch_idx):
        # epoch_idx = self.current_epoch
        x, y = batch
        z = self(x)
        logits = self.prediction_head(z)
        val_loss = F.cross_entropy(logits, y) * (1 - self.lambda_weight)
        self.log('classification_loss/val', val_loss)
        # retrieval metrics will be evaluated on epoch end
        self.val_embeddings.append(z)
        self.val_labels.append(y)
        # update classification accuracy
        self.accuracy.update(logits, y)
        self.accuracy_top_k.update(logits, y)
        # self.my_logger.info(f'validation epoch {epoch_idx} batch {batch_idx} '
        #                     f'classification loss: {loss}')

    def on_validation_epoch_end(self):
        rp = self._compute_rp(self.val_embeddings, self.val_labels)
        adaptive_rp = self._compute_adaptive_rp(self.val_embeddings, self.val_labels)
        self.log('valid_metric/accuracy', self.accuracy.compute())
        self.log('valid_metric/accuracy_top_k', self.accuracy_top_k.compute())
        self.log('valid_metric/retrieval_precision', rp)
        self.log('valid_metric/adaptive_retrieval_precision', adaptive_rp)
        # self.my_logger.info(f'val_accuracy: {self.accuracy.compute()}')
        # self.my_logger.info(f'val_rp: {rp}')
        # self.my_logger.info(f'val_adaptive_rp: {adaptive_rp}')
        self.accuracy.reset()
        self.accuracy_top_k.reset()

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.learning_rate)
        return optimizer

    def _compute_rp(
        self, 
        embs: torch.Tensor, 
        labels: torch.Tensor
    ) -> torch.Tensor:
        embs = torch.cat(embs)
        labels = torch.cat(labels)
        sim_mtx = self.triplet_loss_fn.distance.compute_mat(embs, embs) * \
                torch.tensor(1. if self.triplet_loss_fn.distance.is_inverted else -1.)
        preds = torch.cat(
            [torch.cat((row[:i],row[i+1:])) for i, row in enumerate(sim_mtx)]
        )
        label_mtx = labels[:, None] == labels[None, :]
        target = torch.cat(
            [torch.cat((row[:i],row[i+1:])) for i, row in enumerate(label_mtx)]
        )
        N = embs.shape[0]
        indexes = torch.arange(N * (N - 1)) // (N - 1)
        return self.retrieval_precision(preds, target, indexes)

    def _compute_adaptive_rp(
        self, 
        embs: torch.Tensor, 
        labels: torch.Tensor
    ) -> torch.Tensor:
        embs = torch.cat(embs)
        labels = torch.cat(labels)
        sim_mtx = self.triplet_loss_fn.distance.compute_mat(embs, embs) * \
                torch.tensor(1. if self.triplet_loss_fn.distance.is_inverted else -1.)
        label_mtx = labels[:, None] == labels[None, :]
        r_p = []
        for i in range(len(sim_mtx)):
            preds = torch.cat((sim_mtx[i,:i], sim_mtx[i,i+1:]))
            target = torch.cat((label_mtx[i,:i], label_mtx[i,i+1:]))
            total_relevant_num = int(target.sum())
            top_k = min(total_relevant_num, self.rp_top_k)
            if top_k > 0:
                r_p.append(retrieval_precision(preds, target, top_k=top_k))
        return torch.stack(r_p).mean()

if __name__ == '__main__':

    # example code

    import lightning as L
    L.seed_everything(2024)
    from label_metric.utils.log_utils import setup_logger
    logger = logging.getLogger(__name__)
    setup_logger(logger)

    weight_manager = WeightManager(logger, active = True)

    from label_metric.data_modules import OrchideaSOLDataModule

    dm = OrchideaSOLDataModule(
        dataset_dir = '/data/scratch/acw751/_OrchideaSOL2020_release',
        min_num_per_leaf = 10,
        duration = 1.0,
        train_ratio = 0.8,
        valid_ratio = 0.1,
        logger = logger,
        more_level = 1,
        weight_manager = weight_manager,
        batch_size = 32, 
        num_workers = 11
    )

    dm.setup('fit')

    from label_metric.models import ConvModel, PredictionHead

    backbone_model = ConvModel(
        duration = 1.0,
        conv_out_channels = 128,
        embedding_size = 256,
        sr = 44100,
        n_fft = 2048,
        hop_length = 512,
        power = 1
    )

    prediction_head = PredictionHead(
        embedding_size = 256,
        num_classes = len(dm.train_set.tree.leaves)
    )

    from pytorch_metric_learning.distances import CosineSimilarity

    triplet_loss_fn = TripletLoss(margin=0.1, distance=CosineSimilarity())

    lm = LabelMetricModule(
        backbone_model = backbone_model,
        prediction_head = prediction_head,
        triplet_loss_fn = triplet_loss_fn,
        lambda_weight = 0.95,
        my_logger = logger,
        weight_manager = weight_manager,
        learning_rate = 0.001,
        classification_accuracy_top_k = 5,
        retrieval_precision_top_k = 5
    )

    trainer = L.Trainer(
        max_epochs = 1000, 
        gradient_clip_val = 1.,
        enable_progress_bar = False,
        deterministic = True
    )
    trainer.fit(model = lm, datamodule = dm)

    # dm.setup('fit')
    # train_loader = dm.train_dataloader()
    # valid_loader = dm.val_dataloader()

    # lm.on_train_epoch_start()
    # for i, batch in enumerate(train_loader):
    #     lm.training_step(batch, batch_idx=i)

    # lm.on_validation_epoch_start()
    # for i, batch in enumerate(valid_loader):
    #     lm.validation_step(batch, batch_idx=i)
    # lm.on_validation_epoch_end()
    