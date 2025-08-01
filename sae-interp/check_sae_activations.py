#!/usr/bin/env python3
"""
Quick check of SAE activation distributions to diagnose sparse features.
"""

import torch
import numpy as np
import h5py
from batch_topk_sae import BatchTopKSAE
import matplotlib.pyplot as plt
from glob import glob
import os
from tqdm import tqdm


def check_activations(model_path='trained_sae.pt', data_dir='../lora-activations-dashboard/backend/activations', 
                     n_samples=10000, device='cuda'):
    """Check activation statistics across features"""
    
    # Load model
    checkpoint = torch.load(model_path, map_location=device)
    config = checkpoint['config']
    
    model = BatchTopKSAE(
        d_model=config['d_model'],
        dict_size=config['dict_size'],
        k=config['k']
    ).to(device)
    
    model.load_state_dict(checkpoint['model_state_dict'])
    model.eval()
    
    print(f"Loaded SAE: d_model={config['d_model']}, dict_size={config['dict_size']}, k={config['k']}")
    
    # Collect activation statistics
    n_features = config['dict_size']
    feature_activations = {i: [] for i in range(n_features)}
    total_samples = 0
    
    # Sample from multiple files
    rollout_files = sorted(glob(os.path.join(data_dir, 'rollout_*.h5')))[:10]
    
    for rollout_file in tqdm(rollout_files, desc="Processing rollouts"):
        with h5py.File(rollout_file, 'r') as f:
            activations = f['activations'][:]
            activations_flat = activations.reshape(-1, 192)
            
            # Sample random indices
            n_tokens = len(activations_flat)
            sample_size = min(n_samples // len(rollout_files), n_tokens)
            indices = np.random.choice(n_tokens, sample_size, replace=False)
            
            batch = torch.from_numpy(activations_flat[indices]).float().to(device)
            
            with torch.no_grad():
                sparse_features = model.encode(batch)  # Shape: (batch_size, dict_size)
                
                # Collect non-zero activations for each feature
                for feat_idx in range(n_features):
                    feat_acts = sparse_features[:, feat_idx].cpu().numpy()
                    nonzero_acts = feat_acts[feat_acts > 0]
                    if len(nonzero_acts) > 0:
                        feature_activations[feat_idx].extend(nonzero_acts.tolist())
                
                total_samples += len(batch)
    
    # Compute statistics
    print(f"\nAnalyzed {total_samples} samples")
    print("\nFeature activation statistics:")
    print("-" * 60)
    
    # Count features by activation frequency
    never_active = 0
    rarely_active = 0  # < 1% of samples
    sometimes_active = 0  # 1-10% of samples
    often_active = 0  # > 10% of samples
    
    activation_rates = []
    
    for feat_idx in range(n_features):
        n_active = len(feature_activations[feat_idx])
        rate = n_active / total_samples
        activation_rates.append(rate)
        
        if n_active == 0:
            never_active += 1
        elif rate < 0.01:
            rarely_active += 1
        elif rate < 0.1:
            sometimes_active += 1
        else:
            often_active += 1
    
    print(f"Never active (0%): {never_active} features ({never_active/n_features:.1%})")
    print(f"Rarely active (<1%): {rarely_active} features ({rarely_active/n_features:.1%})")
    print(f"Sometimes active (1-10%): {sometimes_active} features ({sometimes_active/n_features:.1%})")
    print(f"Often active (>10%): {often_active} features ({often_active/n_features:.1%})")
    
    # Show top 10 most active features
    print("\nTop 10 most active features:")
    top_features = sorted(enumerate(activation_rates), key=lambda x: x[1], reverse=True)[:10]
    for feat_idx, rate in top_features:
        n_active = len(feature_activations[feat_idx])
        if n_active > 0:
            mean_act = np.mean(feature_activations[feat_idx])
            max_act = np.max(feature_activations[feat_idx])
            print(f"  Feature {feat_idx}: {rate:.2%} active, mean={mean_act:.3f}, max={max_act:.3f}")
    
    # Plot histogram of activation rates
    plt.figure(figsize=(10, 6))
    plt.hist(activation_rates, bins=50, edgecolor='black')
    plt.xlabel('Activation Rate (fraction of samples)')
    plt.ylabel('Number of Features')
    plt.title('Distribution of Feature Activation Rates')
    plt.yscale('log')
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig('sae_activation_rates.png')
    print("\nSaved activation rate histogram to sae_activation_rates.png")
    
    # Check dead features from training
    if 'dead_latents' in checkpoint:
        dead_from_training = checkpoint['dead_latents']
        print(f"\nDead features from training: {len(dead_from_training)}")
        print(f"Dead features that are still inactive: {sum(1 for i in dead_from_training if activation_rates[i] == 0)}")
    
    return feature_activations, activation_rates


if __name__ == '__main__':
    check_activations()