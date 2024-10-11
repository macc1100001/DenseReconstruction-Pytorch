#!/bin/bash

#tensorboard --logdir log_root_example_1/dense_descriptor_train_1_1_2_27

python3 train_descriptor.py --adjacent_range 1 50 --image_downsampling 4.0 --network_downsampling 64 --input_size 256 320 --batch_size 2 --num_workers 4 --lr_range 1.0e-4 1.0e-3 --inlier_percentage 0.9 --display_interval 50 --validation_interval 2 --training_patient_id 1 --num_epoch 60 --num_iter 1000 --heatmap_sigma 20.0 --visibility_overlap 20 --display_architecture --load_intermediate_data --data_root  "example_training_data_root/" --sampling_size 10 --log_root "log_root_example/" --feature_length 256 --filter_growth_rate 10 --matching_scale 20.0 --matching_threshold 0.9 --rr_weight 1.0 --cross_check_distance 3.0 --precompute_root "precompute_example/"
