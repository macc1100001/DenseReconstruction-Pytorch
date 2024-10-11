'''
Author: Xingtong Liu, Maia Stiber, Jindan Huang, Masaru Ishii, Gregory D. Hager, Russell H. Taylor, and Mathias Unberath

Copyright (C) 2020 Johns Hopkins University - All Rights Reserved
You may use, distribute and modify this code under the
terms of the GNU GENERAL PUBLIC LICENSE Version 3 license for non-commercial usage.

You should have received a copy of the GNU GENERAL PUBLIC LICENSE Version 3 license with
this file. If not, please write to: xliu89@jh.edu or unberath@jhu.edu
'''
import argparse
import multiprocessing
import cv2
from pathlib import Path
import torch
import numpy as np
import random
import datetime
from tensorboardX import SummaryWriter
import torchsummary
import tqdm
import math

# local import
import utils
import dataset
import models
import losses

if __name__ == '__main__':
    multiprocessing.set_start_method('spawn', force=True)
    parser = argparse.ArgumentParser(
        description='Dense descriptor training',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--adjacent_range', nargs='+', type=int, required=True,
                        help='interval range for a pair of video frames')
    parser.add_argument('--image_downsampling', type=float, default=4.0,
                        help='input image downsampling rate for training acceleration')
    parser.add_argument('--network_downsampling', type=int, default=64,
                        help='network downsampling rate')
    parser.add_argument('--input_size', nargs='+', type=int, required=True,
                        help='input size')
    parser.add_argument('--batch_size', type=int, default=8, help='batch size of input samples')
    parser.add_argument('--num_workers', type=int, default=8, help='number of workers for data loader')
    parser.add_argument('--lr_range', nargs='+', type=float, required=True,
                        help='lower and upper bound learning rate for cyclic lr')
    parser.add_argument('--inlier_percentage', type=float, default=0.998,
                        help='percentage of inliers of SfM point clouds (for pruning extreme outliers)')
    parser.add_argument('--display_interval', type=int, default=10, help='iteration interval of image display')
    parser.add_argument('--validation_interval', type=int, default=1, help='iteration interval for validation')
    parser.add_argument('--training_patient_id', nargs='+', type=int, required=True,
                        help='id of the training patient')
    parser.add_argument('--load_intermediate_data', action='store_true',
                        help='whether or not to load intermediate data')
    parser.add_argument('--load_trained_model', action='store_true', help='whether or not to load trained model')
    parser.add_argument('--num_epoch', type=int, required=True, help='number of epochs in total')
    parser.add_argument('--num_iter', type=int, required=True, help='maximum number of iterations per epoch')
    parser.add_argument('--heatmap_sigma', type=float, default=5.0,
                        help='sigma of heatmap for ground truth visualization')
    parser.add_argument('--visibility_overlap', type=int, default=20, help='overlap of point visibility information')
    parser.add_argument('--display_architecture', action='store_true', help='display the network architecture')
    parser.add_argument('--trained_model_path', type=str, default=None, help='path to the trained model')
    parser.add_argument('--data_root', type=str, required=True, help='path to the training data')
    parser.add_argument('--sampling_size', type=int, default=10,
                        help='number of positive sample pairs per iteration')
    parser.add_argument('--log_root', type=str, required=True, help='root of logging')
    parser.add_argument('--feature_length', type=int, default=128, help='output channel dimension of network')
    parser.add_argument('--filter_growth_rate', type=int, default=10, help='filter growth rate of network')
    parser.add_argument('--matching_scale', type=float, default=20.0, help='scale for soft thresholding')
    parser.add_argument('--matching_threshold', type=float, default=0.9, help='threshold for soft thresholding')
    parser.add_argument('--rr_weight', type=float, default=1.0, help='weight of relative response loss')
    parser.add_argument('--cross_check_distance', type=float, default=5.0, help='cross check distance for '
                                                                                'pair-wise feature matching pruning')
    parser.add_argument('--precompute_root', type=str, required=True, help='root of the pre-compute data')
    args = parser.parse_args()

    load_trained_model = args.load_trained_model
    if load_trained_model:
        if args.trained_model_path is not None:
            trained_model_path = Path(args.trained_model_path)
        else:
            raise IOError
    else:
        trained_model_path = None
    log_root = Path(args.log_root)

    current_date = datetime.datetime.now()
    if not Path(args.data_root).exists():
        print("specified training data root does not exist")
        raise IOError

    # Fix randomness for reproducibility
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True
    torch.manual_seed(10085)
    np.random.seed(10085)
    random.seed(10085)

    if not log_root.exists():
        log_root.mkdir()
    log_root = log_root / "dense_descriptor_train_{}_{}_{}_{}".format(current_date.month, current_date.day,
                                                                      current_date.hour,
                                                                      current_date.minute)
    writer = SummaryWriter(logdir=str(log_root))
    print("Created tensorboard visualization at {}".format(str(log_root)))

    if not Path(args.precompute_root).exists():
        Path(args.precompute_root).mkdir(parents=True)

    train_filenames = \
        utils.get_color_file_names_by_bag(root=Path(args.data_root), id_list=args.training_patient_id)

    sequence_path_list = utils.get_parent_folder_names(Path(args.data_root), id_list=args.training_patient_id)

    # Build training and validation dataset
    train_dataset = dataset.DescriptorDataset(image_file_names=train_filenames,
                                              folder_list=sequence_path_list,
                                              adjacent_range=args.adjacent_range,
                                              image_downsampling=args.image_downsampling,
                                              inlier_percentage=args.inlier_percentage,
                                              network_downsampling=args.network_downsampling,
                                              load_intermediate_data=args.load_intermediate_data,
                                              intermediate_data_root=Path(args.precompute_root),
                                              sampling_size=args.sampling_size,
                                              heatmap_sigma=args.heatmap_sigma,
                                              num_pre_workers=args.num_workers,
                                              visible_interval=args.visibility_overlap,
                                              num_iter=args.num_iter)
    train_loader = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=args.batch_size, shuffle=True,
                                               num_workers=args.num_workers)

    val_loader = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=args.batch_size, shuffle=False,
                                             num_workers=args.num_workers)

    feature_descriptor_model = models.FCDenseNetFeature(
        in_channels=3, down_blocks=(3, 3, 3, 3, 3),
        up_blocks=(3, 3, 3, 3, 3), bottleneck_layers=4,
        growth_rate=args.filter_growth_rate, out_chans_first_conv=16, feature_length=args.feature_length)
    # Initialize the network with Kaiming He initialization
    utils.init_net(feature_descriptor_model, type="kaiming", mode="fan_in", activation_mode="relu",
                   distribution="normal")
    # Multi-GPU running
    feature_descriptor_model = torch.nn.DataParallel(feature_descriptor_model)
    # Summary network architecture
    if args.display_architecture:
        torchsummary.summary(feature_descriptor_model, input_size=(3, args.input_size[0], args.input_size[1]))

    # Optimizer
    optimizer = torch.optim.SGD(feature_descriptor_model.parameters(), lr=args.lr_range[1], momentum=0.9)
    lr_scheduler = models.CyclicLR(optimizer, base_lr=args.lr_range[0], max_lr=args.lr_range[1])
    # Loss functions
    response_map_generator = models.FeatureResponseGenerator(scale=args.matching_scale,
                                                             threshold=args.matching_threshold)
    relative_response_loss = losses.RelativeResponseLoss()

    # Validation metric
    matching_accuracy_metric = losses.MatchingAccuracyMetric(threshold=5)

    # Load trained model
    if load_trained_model:
        if trained_model_path.exists():
            print("Loading {:s} ...".format(str(trained_model_path)))
            pre_trained_state = torch.load(str(trained_model_path))
            step = pre_trained_state['step']
            epoch = pre_trained_state['epoch']
            model_state = feature_descriptor_model.state_dict()
            trained_model_state = {k: v for k, v in pre_trained_state["model"].items() if k in model_state}
            model_state.update(trained_model_state)
            feature_descriptor_model.load_state_dict(model_state)
            print('Restored model, epoch {}, step {}'.format(epoch, step))
        else:
            print("No trained model detected")
            raise OSError
    else:
        epoch = 0
        step = 0

    validation_step = 0

    sift = cv2.SIFT_create(nfeatures=1000, nOctaveLayers=8,
                                       contrastThreshold=0.00005,
                                       edgeThreshold=100, sigma=1.1)
    for cur_epoch in range(epoch, args.num_epoch + 1):
        # Set the seed correlated to epoch for reproducibility
        torch.manual_seed(10086 + cur_epoch)
        np.random.seed(10086 + cur_epoch)
        random.seed(10086 + cur_epoch)

        feature_descriptor_model.train()
        # Update progress bar
        tq = tqdm.tqdm(total=len(train_loader) * args.batch_size)
        for batch, (
                colors_1, colors_2,
                feature_1D_locations_1,
                feature_1D_locations_2,
                feature_2D_locations_1,
                feature_2D_locations_2,
                gt_heatmaps_1,
                gt_heatmaps_2,
                boundaries,
                folders, names
        ) in \
                enumerate(train_loader):
            # Update learning rate
            lr_scheduler.batch_step(batch_iteration=step)
            tq.set_description('Epoch {}, lr {}'.format(cur_epoch, lr_scheduler.get_lr()))

            colors_1, colors_2, \
            feature_1D_locations_1, feature_1D_locations_2, \
            feature_2D_locations_1, feature_2D_locations_2, \
            boundaries = colors_1.cuda(), colors_2.cuda(), \
                         feature_1D_locations_1.cuda(), feature_1D_locations_2.cuda(), \
                         feature_2D_locations_1.cuda(), feature_2D_locations_2.cuda(), \
                         boundaries.cuda()

            feature_maps_1 = feature_descriptor_model(colors_1)
            feature_maps_2 = feature_descriptor_model(colors_2)

            response_map_2 = response_map_generator(
                [feature_maps_1, feature_maps_2, feature_1D_locations_1, boundaries])
            response_map_1 = response_map_generator(
                [feature_maps_2, feature_maps_1, feature_1D_locations_2, boundaries])

            rr_loss_1 = relative_response_loss(
                [response_map_1, feature_1D_locations_1, boundaries])
            rr_loss_2 = relative_response_loss(
                [response_map_2, feature_1D_locations_2, boundaries])

            rr_loss = args.rr_weight * (0.5 * rr_loss_1 + 0.5 * rr_loss_2)

            # Handle nan cases
            if math.isnan(rr_loss.item()) or math.isinf(rr_loss.item()):
                optimizer.zero_grad()
                rr_loss.backward()
                optimizer.zero_grad()
                tq.update(args.batch_size)
                continue
            else:
                optimizer.zero_grad()
                rr_loss.backward()
                torch.nn.utils.clip_grad_norm_(feature_descriptor_model.parameters(), 10.0)
                optimizer.step()

                if batch == 0:
                    mean_rr_loss = np.mean(rr_loss.item())
                else:
                    mean_rr_loss = (mean_rr_loss * batch + rr_loss.item()) / (batch + 1.0)

            # Result display
            if batch % args.display_interval == 0:
                with torch.no_grad():
                    gt_heatmaps_1 = gt_heatmaps_1.cuda()
                    gt_heatmaps_2 = gt_heatmaps_2.cuda()
                    display_success = utils.display_results(colors_1, colors_2, feature_maps_1, feature_maps_2,
                                                            boundaries, response_map_1,
                                                            gt_heatmaps_1, response_map_2, gt_heatmaps_2,
                                                            sift, args.cross_check_distance, step,
                                                            writer, phase="Train")

            step += 1
            tq.update(colors_1.shape[0])
            tq.set_postfix(loss='average: {:.5f}, current: {:.5f}'.format(mean_rr_loss, rr_loss.item())
                           )
            writer.add_scalars('Train', {'loss': mean_rr_loss}, step)

        tq.close()

        if cur_epoch % args.validation_interval != 0:
            continue

        # Validation
        feature_descriptor_model.eval()
        # Update progress bar
        tq = tqdm.tqdm(total=len(val_loader) * args.batch_size)
        torch.manual_seed(10086)
        np.random.seed(10086)
        random.seed(10086)
        with torch.no_grad():
            for batch, (
                    colors_1, colors_2,
                    feature_1D_locations_1,
                    feature_1D_locations_2,
                    feature_2D_locations_1,
                    feature_2D_locations_2,
                    gt_heatmaps_1,
                    gt_heatmaps_2,
                    boundaries,
                    folders, names
            ) in enumerate(val_loader):
                tq.set_description('Validation Epoch {}'.format(cur_epoch))

                colors_1, colors_2, \
                feature_1D_locations_1, feature_1D_locations_2, \
                feature_2D_locations_1, feature_2D_locations_2, \
                boundaries = colors_1.cuda(), colors_2.cuda(), \
                             feature_1D_locations_1.cuda(), feature_1D_locations_2.cuda(), \
                             feature_2D_locations_1.cuda(), feature_2D_locations_2.cuda(), \
                             boundaries.cuda()

                feature_maps_1 = feature_descriptor_model(colors_1)
                feature_maps_2 = feature_descriptor_model(colors_2)

                response_map_2 = response_map_generator(
                    [feature_maps_1, feature_maps_2, feature_1D_locations_1, boundaries])
                response_map_1 = response_map_generator(
                    [feature_maps_2, feature_maps_1, feature_1D_locations_2, boundaries])

                # Result display
                if batch % args.display_interval == 0:
                    gt_heatmaps_1 = gt_heatmaps_1.cuda()
                    gt_heatmaps_2 = gt_heatmaps_2.cuda()

                    display_success = utils.display_results(colors_1, colors_2, feature_maps_1, feature_maps_2,
                                                            boundaries, response_map_1,
                                                            gt_heatmaps_1, response_map_2, gt_heatmaps_2,
                                                            sift, args.cross_check_distance, validation_step,
                                                            writer, phase="Validation")

                ratio_1, ratio_2, ratio_3 = matching_accuracy_metric(
                    [response_map_1, feature_2D_locations_1, boundaries])
                ratio_4, ratio_5, ratio_6 = matching_accuracy_metric(
                    [response_map_2, feature_2D_locations_2, boundaries])
                accuracy_1 = 0.5 * ratio_1 + 0.5 * ratio_4
                accuracy_2 = 0.5 * ratio_2 + 0.5 * ratio_5
                accuracy_3 = 0.5 * ratio_3 + 0.5 * ratio_6

                if batch == 0:
                    mean_accuracy_1 = np.mean(accuracy_1.item())
                    mean_accuracy_2 = np.mean(accuracy_2.item())
                    mean_accuracy_3 = np.mean(accuracy_3.item())
                else:
                    mean_accuracy_1 = (mean_accuracy_1 * batch + accuracy_1.item()) / (batch + 1.0)
                    mean_accuracy_2 = (mean_accuracy_2 * batch + accuracy_2.item()) / (batch + 1.0)
                    mean_accuracy_3 = (mean_accuracy_3 * batch + accuracy_3.item()) / (batch + 1.0)

                validation_step += 1
                tq.update(colors_1.shape[0])
                tq.set_postfix(
                    accuracy_1='average: {:.5f}, current: {:.5f}'.format(mean_accuracy_1, accuracy_1.item()),
                    accuracy_2='average: {:.5f}, current: {:.5f}'.format(mean_accuracy_2, accuracy_2.item()),
                    accuracy_3='average: {:.5f}, current: {:.5f}'.format(mean_accuracy_3, accuracy_3.item())
                )
                writer.add_scalars('Validation', {'accuracy_1': mean_accuracy_1, 'accuracy_2': mean_accuracy_2,
                                                  'accuracy_3': mean_accuracy_3}, validation_step)
            tq.close()
            model_path_epoch = log_root / \
                               'checkpoint_model_epoch_{}_{}_{}_{}.pt'.format(cur_epoch, mean_accuracy_1,
                                                                              mean_accuracy_2, mean_accuracy_3)
            utils.save_model(model=feature_descriptor_model, optimizer=optimizer,
                             epoch=cur_epoch + 1, step=step, model_path=model_path_epoch,
                             validation_loss=mean_accuracy_1)

        writer.close()
