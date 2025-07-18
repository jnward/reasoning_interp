# %%
import torch
import numpy as np
from safetensors import safe_open
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pathlib import Path
import glob

# %%
# Configuration
lora_path = "/workspace/models/ckpts_1.1"
rank = 1  # Analyzing rank-1 LoRA

# Find the rank-1 LoRA checkpoint
lora_dirs = glob.glob(f"{lora_path}/s1-lora-32B-r{rank}-*")
if not lora_dirs:
    raise ValueError(f"No rank-{rank} LoRA found in {lora_path}")

lora_dir = sorted(lora_dirs)[-1]  # Get the most recent one
print(f"Loading LoRA from: {lora_dir}")

# %%
# Load LoRA weights from safetensors
adapter_path = Path(lora_dir) / "adapter_model.safetensors"
if not adapter_path.exists():
    raise FileNotFoundError(f"adapter_model.safetensors not found in {lora_dir}")

lora_weights = {}
with safe_open(adapter_path, framework="pt", device="cpu") as f:
    for key in f.keys():
        lora_weights[key] = f.get_tensor(key)

print(f"Loaded {len(lora_weights)} weight tensors")
print("Sample keys:", list(lora_weights.keys())[:10])

# %%
# Group weights by type
# For rank-1 LoRA: W = W_0 + BA^T where B is d×r and A is r×k
# We're interested in the direction vectors (columns of B for down projections)

weight_groups = {
    'q_proj': {},
    'k_proj': {},
    'v_proj': {},
    'o_proj': {},
    'gate_proj': {},
    'up_proj': {},
    'down_proj': {}
}

# First, let's examine the key structure
print("\nExamining key structure:")
for i, key in enumerate(list(lora_weights.keys())[:5]):
    print(f"  {key}")

for key, tensor in lora_weights.items():
    for proj_type in weight_groups.keys():
        if f'.{proj_type}.' in key:
            # Parse the layer number from the key
            parts = key.split('.')
            # Find the numeric part that represents the layer
            for i, part in enumerate(parts):
                if part.isdigit():
                    layer_num = int(part)
                    break
            
            if 'lora_A' in key:
                weight_groups[proj_type][f'layer_{layer_num}_A'] = tensor
            elif 'lora_B' in key:
                weight_groups[proj_type][f'layer_{layer_num}_B'] = tensor

# %%
# For rank-1 LoRA, extract the direction vectors
# For down_proj: B matrices (what layers write to residual stream) have shape [d_model, rank=1]
# For up_proj/gate_proj: A matrices (what layers read from residual stream) have shape [rank=1, d_model]

direction_vectors = {}

# Only process MLP projections
mlp_projections = ['down_proj', 'up_proj', 'gate_proj']

for proj_type, weights in weight_groups.items():
    if not weights or proj_type not in mlp_projections:
        continue
        
    vectors = []
    layer_indices = []
    
    for i in range(64):  # Qwen-2.5-32B has 64 layers
        if proj_type == 'down_proj':
            # For down_proj, use B matrix (what is written to residual stream)
            b_key = f'layer_{i}_B'
            if b_key in weights:
                # B matrix shape: [d_model, 1] for rank-1
                vector = weights[b_key].squeeze(-1)  # Remove rank dimension
                vectors.append(vector)
                layer_indices.append(i)
        elif proj_type in ['up_proj', 'gate_proj']:
            # For up_proj/gate_proj, use A matrix (what is read from residual stream)
            a_key = f'layer_{i}_A'
            if a_key in weights:
                # A matrix shape: [1, d_model] for rank-1
                vector = weights[a_key].squeeze(0)  # Remove rank dimension
                vectors.append(vector)
                layer_indices.append(i)
    
    if vectors:
        direction_vectors[proj_type] = {
            'vectors': torch.stack(vectors),  # Shape: [n_layers, d_model]
            'layers': layer_indices
        }

print("Direction vectors extracted for:")
for proj_type, data in direction_vectors.items():
    print(f"  {proj_type}: {data['vectors'].shape}")

# %%
# Compute cosine similarity matrices
def compute_cosine_similarity(vectors):
    """Compute cosine similarity between all pairs of vectors."""
    # Normalize vectors
    norms = torch.norm(vectors, dim=1, keepdim=True)
    normalized = vectors / (norms + 1e-8)
    
    # Compute cosine similarity
    similarity = torch.mm(normalized, normalized.t())
    return similarity.numpy()

cosine_similarities = {}
for proj_type, data in direction_vectors.items():
    cosine_similarities[proj_type] = compute_cosine_similarity(data['vectors'])

# %%
# Compute cross-projection cosine similarities
def compute_cross_projection_similarity(vectors1, vectors2):
    """Compute cosine similarity between vectors from two different projections."""
    # Normalize vectors
    norms1 = torch.norm(vectors1, dim=1, keepdim=True)
    norms2 = torch.norm(vectors2, dim=1, keepdim=True)
    normalized1 = vectors1 / (norms1 + 1e-8)
    normalized2 = vectors2 / (norms2 + 1e-8)
    
    # Compute cosine similarity
    similarity = torch.mm(normalized1, normalized2.t())
    return similarity.numpy()

# Compute cross-projection similarities
cross_similarities = {}

# up_proj vs down_proj
if 'up_proj' in direction_vectors and 'down_proj' in direction_vectors:
    cross_similarities['up_proj_vs_down_proj'] = compute_cross_projection_similarity(
        direction_vectors['up_proj']['vectors'],
        direction_vectors['down_proj']['vectors']
    )

# gate_proj vs down_proj
if 'gate_proj' in direction_vectors and 'down_proj' in direction_vectors:
    cross_similarities['gate_proj_vs_down_proj'] = compute_cross_projection_similarity(
        direction_vectors['gate_proj']['vectors'],
        direction_vectors['down_proj']['vectors']
    )

# up_proj vs gate_proj
if 'up_proj' in direction_vectors and 'gate_proj' in direction_vectors:
    cross_similarities['up_proj_vs_gate_proj'] = compute_cross_projection_similarity(
        direction_vectors['up_proj']['vectors'],
        direction_vectors['gate_proj']['vectors']
    )

print("\nCross-projection similarity matrices computed:")
for name, matrix in cross_similarities.items():
    print(f"  {name}: {matrix.shape}")

# %%
# Plot cosine similarity matrices for MLP projections
mlp_cosine_similarities = {k: v for k, v in cosine_similarities.items() if k in mlp_projections}

fig = make_subplots(
    rows=1, cols=3,
    subplot_titles=list(mlp_cosine_similarities.keys()),
    horizontal_spacing=0.1
)

for idx, (proj_type, sim_matrix) in enumerate(mlp_cosine_similarities.items()):
    col = idx + 1
    
    heatmap = go.Heatmap(
        z=sim_matrix,
        colorscale='RdBu',
        zmid=0,
        zmin=-1,
        zmax=1,
        colorbar=dict(len=0.8, y=0.5) if col == 3 else dict(showticklabels=False),
        showscale=(col == 3)  # Only show colorbar for rightmost plots
    )
    
    fig.add_trace(heatmap, row=1, col=col)
    
    # Update axes
    fig.update_xaxes(
        title_text="Layer",
        tickmode='linear',
        dtick=8,
        row=1, col=col
    )
    fig.update_yaxes(
        title_text="Layer",
        tickmode='linear',
        dtick=8,
        row=1, col=col
    )

fig.update_layout(
    title=f"Cosine Similarity of Rank-{rank} LoRA Direction Vectors (MLP Projections)",
    width=1200,
    height=400,
    showlegend=False
)

fig.show()

# %%
# Plot magnitude of direction vectors across layers
magnitudes = {}
for proj_type, data in direction_vectors.items():
    # Compute L2 norm of each direction vector
    mags = torch.norm(data['vectors'], dim=1).numpy()
    magnitudes[proj_type] = mags

# Create line plot
fig = go.Figure()
for proj_type, mags in magnitudes.items():
    fig.add_trace(go.Scatter(
        x=list(range(len(mags))),
        y=mags,
        mode='lines+markers',
        name=proj_type,
        line=dict(width=2),
        marker=dict(size=4)
    ))

fig.update_layout(
    title=f"Magnitude of LoRA Direction Vectors by Layer (Rank-{rank})",
    xaxis_title="Layer",
    yaxis_title="L2 Norm",
    width=1000,
    height=600,
    hovermode='x unified'
)

fig.show()

# %%
# Plot cross-projection similarity matrices
fig = make_subplots(
    rows=1, cols=3,
    subplot_titles=list(cross_similarities.keys()),
    horizontal_spacing=0.1
)

for idx, (name, sim_matrix) in enumerate(cross_similarities.items()):
    col = idx + 1
    
    heatmap = go.Heatmap(
        z=sim_matrix,
        colorscale='RdBu',
        zmid=0,
        zmin=-1,
        zmax=1,
        colorbar=dict(len=0.8, y=0.5) if col == 3 else dict(showticklabels=False),
        showscale=(col == 3)  # Only show colorbar for rightmost plot
    )
    
    fig.add_trace(heatmap, row=1, col=col)
    
    # Update axes
    proj1, proj2 = name.split('_vs_')
    fig.update_xaxes(
        title_text=f"{proj2} Layer",
        tickmode='linear',
        dtick=8,
        row=1, col=col
    )
    fig.update_yaxes(
        title_text=f"{proj1} Layer",
        tickmode='linear',
        dtick=8,
        row=1, col=col
    )

fig.update_layout(
    title=f"Cross-Projection Cosine Similarity of Rank-{rank} LoRA Direction Vectors",
    width=1400,
    height=500,
    showlegend=False
)

fig.show()

# %%
# Compute statistics for each projection type
print("\nWithin-Projection Cosine Similarity Statistics:\n")
for proj_type, sim_matrix in mlp_cosine_similarities.items():
    # Get off-diagonal elements
    n = sim_matrix.shape[0]
    mask = ~np.eye(n, dtype=bool)
    off_diagonal = sim_matrix[mask]
    
    print(f"{proj_type}:")
    print(f"  Mean similarity: {off_diagonal.mean():.3f}")
    print(f"  Std deviation: {off_diagonal.std():.3f}")
    print(f"  Min similarity: {off_diagonal.min():.3f}")
    print(f"  Max similarity: {off_diagonal.max():.3f}")
    print()

# %%
# Compute statistics for cross-projection similarities
print("\nCross-Projection Cosine Similarity Statistics:\n")
for name, sim_matrix in cross_similarities.items():
    # All elements are cross-projection (no diagonal to exclude)
    flat_matrix = sim_matrix.flatten()
    
    print(f"{name}:")
    print(f"  Mean similarity: {flat_matrix.mean():.3f}")
    print(f"  Std deviation: {flat_matrix.std():.3f}")
    print(f"  Min similarity: {flat_matrix.min():.3f}")
    print(f"  Max similarity: {flat_matrix.max():.3f}")
    print()

# %%
