"""
Training dictionaries
"""

import torch as t
from .dictionary import AutoEncoder
import os
from tqdm import tqdm

EPS = 1e-8

class ConstrainedAdam(t.optim.Adam):
    """
    A variant of Adam where some of the parameters are constrained to have unit norm.
    """
    def __init__(self, params, constrained_params, lr):
        super().__init__(params, lr=lr)
        self.constrained_params = list(constrained_params)
    
    def step(self, closure=None):
        with t.no_grad():
            for p in self.constrained_params:
                normed_p = p / p.norm(dim=0, keepdim=True)
                # project away the parallel component of the gradient
                p.grad -= (p.grad * normed_p).sum(dim=0, keepdim=True) * normed_p
        super().step(closure=closure)
        with t.no_grad():
            for p in self.constrained_params:
                # renormalize the constrained parameters
                p /= p.norm(dim=0, keepdim=True)

def entropy(p, eps=1e-8):
    p_sum = p.sum(dim=-1, keepdim=True)
    # epsilons for numerical stability    
    p_normed = p / (p_sum + eps)    
    p_log = t.log(p_normed + eps)
    ent = -(p_normed * p_log)
    
    # Zero out the entropy where p_sum is zero
    ent = t.where(p_sum > 0, ent, t.zeros_like(ent))

    return ent.sum(dim=-1).mean()


def sae_loss(activations, ae, sparsity_penalty, use_entropy=False, separate=False, num_samples_since_activated=None, ghost_threshold=None):
    """
    Compute the loss of an autoencoder on some activations
    If separate is True, return the MSE loss, the sparsity loss, and the ghost loss separately
    If num_samples_since_activated is not None, update it in place
    If ghost_threshold is not None, use it to do ghost grads
    """
    if isinstance(activations, tuple): # for cases when the input to the autoencoder is not the same as the output
        in_acts, out_acts = activations
    else: # typically the input to the autoencoder is the same as the output
        in_acts = out_acts = activations
    
    ghost_grads = False
    if ghost_threshold is not None:
        if num_samples_since_activated is None:
            raise ValueError("num_samples_since_activated must be provided for ghost grads")
        ghost_mask = num_samples_since_activated > ghost_threshold
        if ghost_mask.sum() > 0: # if there are dead neurons
            ghost_grads = True
        else:
            ghost_loss = None

    if not ghost_grads: # if we're not doing ghost grads
        x_hat, f = ae(in_acts, output_features=True)
        mse_loss = t.linalg.norm(out_acts - x_hat, dim=-1).mean() / (out_acts).norm(dim=-1).mean()        
    
    else: # if we're doing ghost grads        
        x_hat, x_ghost, f = ae(in_acts, output_features=True, ghost_mask=ghost_mask)
        residual = out_acts - x_hat
        mse_loss = t.linalg.norm(residual, dim=-1).mean()
        x_ghost = x_ghost * residual.norm(dim=-1, keepdim=True).detach() / (2 * x_ghost.norm(dim=-1, keepdim=True).detach() + EPS)
        ghost_loss = t.linalg.norm(residual.detach() - x_ghost, dim=-1).mean()

    if num_samples_since_activated is not None: # update the number of samples since each neuron was last activated
        deads = (f == 0).all(dim=0)
        num_samples_since_activated.copy_(
            t.where(
                deads,
                num_samples_since_activated + 1,
                0
            )
        )
    
    if use_entropy:
        sparsity_loss = entropy(f)
    else:
        sparsity_loss = f.norm(p=1, dim=-1).mean()

    
    
    if separate:
        #metrics
        metrics = dict()
        L2 = out_acts.norm(dim=1)
        L1 = out_acts.abs().sum(dim=1)
        zero_act = L2 == 0
        metrics['approx_L0'] = ((L1[~zero_act]/L2[~zero_act])**2).mean()
        metrics['zero_act_frac'] = zero_act.sum()/len(L2)
        metrics['L0'] = (activations[~zero_act] > 1e-5).sum(dim=1, dtype=t.float).mean()
        
        if ghost_threshold is None:
            return mse_loss, sparsity_loss, metrics
        else:
            return mse_loss, sparsity_loss, ghost_loss, metrics
    else:
        if not ghost_grads:
            return mse_loss + sparsity_penalty * sparsity_loss
        else:
            return mse_loss + sparsity_penalty * sparsity_loss + ghost_loss * (mse_loss.detach() / (ghost_loss.detach() + EPS))

    
def resample_neurons(deads, activations, ae, optimizer):
    """
    resample dead neurons according to the following scheme:
    Reinitialize the decoder vector for each dead neuron to be an activation
    vector v from the dataset with probability proportional to ae's loss on v.
    Reinitialize all dead encoder vectors to be the mean alive encoder vector x 0.2.
    Reset the bias vectors for dead neurons to 0.
    Reset the Adam parameters for the dead neurons to their default values.
    """
    with t.no_grad():
        if deads.sum() == 0:
            return
        if isinstance(activations, tuple):
            in_acts, out_acts = activations
        else:
            in_acts = out_acts = activations
        in_acts = in_acts.reshape(-1, in_acts.shape[-1])
        out_acts = out_acts.reshape(-1, out_acts.shape[-1])

        # compute the loss for each activation vector
        losses = (out_acts - ae(in_acts)).norm(dim=-1)

        # sample inputs to create encoder/decoder weights from
        n_resample = min([deads.sum(), losses.shape[0]])
        indices = t.multinomial(losses, num_samples=n_resample, replacement=False)
        sampled_vecs = out_acts[indices]

        alive_norm = ae.encoder.weight[~deads].norm(dim=-1).mean()
        ae.encoder.weight[deads][:n_resample] = sampled_vecs * alive_norm * 0.2
        ae.decoder.weight[:,deads][:,:n_resample] = (sampled_vecs / sampled_vecs.norm(dim=-1, keepdim=True)).T
        # reset bias vectors for dead neurons
        ae.encoder.bias[deads][:n_resample] = 0.

        # reset Adam parameters for dead neurons
        state_dict = optimizer.state_dict()['state']
        # # encoder weight
        state_dict[1]['exp_avg'][deads] = 0.
        state_dict[1]['exp_avg_sq'][deads] = 0.
        # # encoder bias
        state_dict[2]['exp_avg'][deads] = 0.
        state_dict[2]['exp_avg_sq'][deads] = 0.


def trainSAE(
        activations, # a generator that outputs batches of activations
        activation_dim, # dimension of the activations
        dictionary_size, # size of the dictionary
        lr,
        sparsity_penalty,
        entropy=False,
        steps=None, # if None, train until activations are exhausted
        warmup_steps=1000, # linearly increase the learning rate for this many steps
        resample_steps=None, # how often to resample dead neurons
        ghost_threshold=None, # how many steps a neuron has to be dead for it to turn into a ghost
        save_steps=None, # how often to save checkpoints
        save_dir=None, # directory for saving checkpoints
        log_steps=1000, # how often to print statistics
        device='cpu'):
    """
    Train and return a sparse autoencoder
    """
    ae = AutoEncoder(activation_dim, dictionary_size).to(device)
    num_samples_since_activated = t.zeros(dictionary_size, dtype=int).to(device) # how many samples since each neuron was last activated?

    # set up optimizer and scheduler
    optimizer = ConstrainedAdam(ae.parameters(), ae.decoder.parameters(), lr=lr)
    if resample_steps is None:
        def warmup_fn(step):
            return min(step / warmup_steps, 1.)
    else:
        def warmup_fn(step):
            return min((step % resample_steps) / warmup_steps, 1.)
    scheduler = t.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_fn)

    for step, acts in enumerate(tqdm(activations, total=steps)):
        if steps is not None and step >= steps:
            break

        if isinstance(acts, t.Tensor): # typical casse
            acts = acts.to(device)
        elif isinstance(acts, tuple): # for cases where the autoencoder input and output are different
            acts = tuple(a.to(device) for a in acts)

        optimizer.zero_grad()
        # computing the sae_loss also updates num_samples_since_activated in place
        loss = sae_loss(acts, ae, sparsity_penalty, use_entropy=entropy, num_samples_since_activated=num_samples_since_activated, ghost_threshold=ghost_threshold)
        loss.backward()
        optimizer.step()
        scheduler.step()

        # deal with resampling neurons
        if resample_steps is not None and step % resample_steps == 0:
            # resample neurons who've been dead for the last resample_steps / 2 steps
            resample_neurons(num_samples_since_activated > resample_steps / 2, acts, ae, optimizer)

        # logging
        if log_steps is not None and step % log_steps == 0:
            with t.no_grad():
                losses = sae_loss(acts, ae, sparsity_penalty, entropy, separate=True, num_samples_since_activated=num_samples_since_activated, ghost_threshold=ghost_threshold)
                if ghost_threshold is None:
                    mse_loss, sparsity_loss, metrics = losses
                    print(f"step {step} MSE loss: {mse_loss:.4f}, L1 loss: {sparsity_loss:.4f}, L0: {metrics['L0']:.4f}, Approx L0: {metrics['approx_L0']:.4f}, Zero Act. Samples: {metrics['zero_act_frac']:.4f}")
                else:
                    mse_loss, sparsity_loss, ghost_loss, metrics = losses
                    print(f"step {step} MSE loss: {mse_loss}, L1 loss: {sparsity_loss}, ghost_loss: {ghost_loss}, L0: {metrics['L0']:.4f}, Approx L0: {metrics['approx_L0']:.4f}, Zero Act. Samples: {metrics['zero_act_frac']:.4f}")
                # dict_acts = ae.encode(acts)
                # print(f"step {step} % inactive: {(dict_acts == 0).all(dim=0).sum() / dict_acts.shape[-1]}")
                # if isinstance(activations, ActivationBuffer):
                #     tokens = activations.tokenized_batch().input_ids
                #     loss_orig, loss_reconst, loss_zero = reconstruction_loss(tokens, activations.model, activations.submodule, ae)
                #     print(f"step {step} reconstruction loss: {loss_orig}, {loss_reconst}, {loss_zero}")

        # saving
        if save_steps is not None and save_dir is not None and step % save_steps == 0:
            if not os.path.exists(os.path.join(save_dir, "checkpoints")):
                os.mkdir(os.path.join(save_dir, "checkpoints"))
            t.save(
                ae.state_dict(), 
                os.path.join(save_dir, "checkpoints", f"ae_{step}.pt")
                )

    return ae
