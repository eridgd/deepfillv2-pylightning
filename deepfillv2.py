import pytorch_lightning as pl
import torch
import numpy as np
from model.InpaintSAGenerator import InpaintSAGenerator
from model.InpaintSADiscriminator import InpaintSADiscriminator
from dataset import InpaintDataset
from util.loss import ReconstructionLoss


torch.backends.cudnn.benchmark = True


class DeepFillV2(pl.LightningModule):

    def __init__(self, args):
        super(DeepFillV2, self).__init__()
        self.hparams = args
        self.net_G = InpaintSAGenerator(args.input_nc)
        self.net_D = InpaintSADiscriminator(args.input_nc)
        self.recon_loss = ReconstructionLoss(args.l1_c_h, args.l1_c_nh, args.l1_r_h, args.l1_r_nh)
        self.last_batch = None
        self.generated_image = None
        self.generated_image_only_patch = None
        self.visualization_dataloader = self.setup_dataloader_for_visualizations()

    def configure_optimizers(self):
        lr = self.hparams.lr
        decay = self.hparams.weight_decay
        opt_g = torch.optim.Adam(self.net_G.parameters(), lr=lr, weight_decay=decay)
        opt_d = torch.optim.Adam(self.net_D.parameters(), lr=4 * lr, weight_decay=decay)
        return [opt_g, opt_d], []

    @pl.data_loader
    def train_dataloader(self):
        dataset = InpaintDataset.InpaintDataset(self.hparams.dataset, "train", self.hparams.image_size, self.hparams.bbox_shape, self.hparams.bbox_randomness, self.hparams.bbox_margin, self.hparams.bbox_max_num, self.hparams.overfit)
        return torch.utils.data.DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.num_workers, drop_last=True)

    @pl.data_loader
    def val_dataloader(self):
        dataset = InpaintDataset.InpaintDataset(self.hparams.dataset, "val", self.hparams.image_size, self.hparams.bbox_shape, self.hparams.bbox_randomness, self.hparams.bbox_margin, self.hparams.bbox_max_num, False)
        return torch.utils.data.DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.num_workers, drop_last=False)

    def setup_dataloader_for_visualizations(self):
        dataset = InpaintDataset.InpaintDataset(self.hparams.dataset, self.hparams.vis_dataset, self.hparams.image_size, self.hparams.bbox_shape, self.hparams.bbox_randomness, self.hparams.bbox_margin, self.hparams.bbox_max_num, False)
        return torch.utils.data.DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.num_workers, drop_last=False)

    def training_step(self, batch, batch_idx, optimizer_idx):
        image = batch['image']
        mask = batch['mask']
        self.last_batch = batch
        if optimizer_idx == 0:
            # generator training
            coarse_image, refined_image = self.net_G(image, mask)
            self.generated_image = refined_image
            reconstruction_loss = self.recon_loss(image, coarse_image, refined_image, mask)
            completed_image = refined_image * mask + image * (1 - mask)
            self.generated_image_only_patch = completed_image
            d_fake = self.net_D(torch.cat((completed_image, mask), dim=1))
            gen_loss = -self.hparams.gen_loss_alpha * torch.mean(d_fake)
            total_loss = gen_loss + reconstruction_loss
            self.logger.log_generator_losses(self.global_step, gen_loss, reconstruction_loss)
            self.logger.log_total_generator_loss(self.global_step, total_loss)
            return {
                'loss': total_loss,
                'progress_bar': {
                    'gen_loss': gen_loss,
                    'recon_loss': reconstruction_loss
                }
            }
        if optimizer_idx == 1:
            d_real = self.net_D(torch.cat((image, mask), dim=1))
            d_fake = self.net_D(torch.cat((self.generated_image_only_patch.detach(), mask), dim=1))
            real_loss = torch.mean(torch.nn.functional.relu(1. - d_real))
            fake_loss = torch.mean(torch.nn.functional.relu(1. + d_fake))
            disc_loss = self.hparams.disc_loss_alpha * (real_loss + fake_loss)
            self.logger.log_total_discriminator_loss(self.global_step, disc_loss)
            self.logger.log_discriminator_losses(self.global_step, real_loss, fake_loss)
            return {
                'loss': disc_loss,
                'progress_bar': {
                    'd_real_loss': real_loss,
                    'd_fake_loss': fake_loss
                }
            }

    def on_epoch_end(self):
        images = []
        coarse = []
        refined = []
        masked = []
        completed = []
        with torch.no_grad():
            for t, batch in enumerate(self.visualization_dataloader):
                batch['image'] = batch['image'].cuda()
                batch['mask'] = batch['mask'].cuda()
                coarse_image, refined_image = self.net_G(batch['image'], batch['mask'])
                completed_image = (refined_image * batch['mask'] + batch['image'] * (1 - batch['mask'])).cpu().numpy()
                coarse_image = coarse_image.cpu().numpy()
                refined_image = refined_image.cpu().numpy()
                masked_image = batch['image'] * (1 - batch['mask']) + batch['mask']
                masked_image = masked_image.cpu().numpy()

                for j in range(batch['image'].size(0)):
                    images.append(((np.transpose(batch['image'][j].cpu().numpy(), axes=[1, 2, 0]) * (0.229, 0.224, 0.225) + (0.485, 0.456, 0.406)) * 255).astype(np.uint8))
                    coarse.append(((np.transpose(coarse_image[j], axes=[1, 2, 0]) * (0.229, 0.224, 0.225) + (0.485, 0.456, 0.406)) * 255).astype(np.uint8))
                    refined.append(((np.transpose(refined_image[j], axes=[1, 2, 0]) * (0.229, 0.224, 0.225) + (0.485, 0.456, 0.406)) * 255).astype(np.uint8))
                    masked.append(((np.transpose(masked_image[j], axes=[1, 2, 0]) * (0.229, 0.224, 0.225) + (0.485, 0.456, 0.406)) * 255).astype(np.uint8))
                    completed.append(((np.transpose(completed_image[j], axes=[1, 2, 0]) * (0.229, 0.224, 0.225) + (0.485, 0.456, 0.406)) * 255).astype(np.uint8))
            visualization = np.hstack([np.vstack(masked), np.vstack(coarse), np.vstack(refined), np.vstack(completed), np.vstack(images)])
            self.logger.log_image(self.global_step, visualization)


if __name__ == '__main__':
    from util import arguments
    from util.logger import NestedFolderTensorboardLogger
    from pytorch_lightning import Trainer
    from util import constants
    from pytorch_lightning.callbacks import ModelCheckpoint
    import os

    args = arguments.parse_arguments()

    logger = NestedFolderTensorboardLogger(save_dir=os.path.join(constants.RUNS_FOLDER, args.dataset), name=args.experiment)
    checkpoint_callback = ModelCheckpoint(filepath=os.path.join(constants.RUNS_FOLDER, args.dataset, args.experiment), save_best_only=False, verbose=False, period=args.save_epoch)

    model = DeepFillV2(args)

    trainer = Trainer(gpus=[0], early_stop_callback=None, nb_sanity_val_steps=3, logger=logger, checkpoint_callback=checkpoint_callback, max_nb_epochs=args.max_epoch, check_val_every_n_epoch=2)

    trainer.fit(model)
