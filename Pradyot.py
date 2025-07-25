# -*- coding: utf-8 -*-
"""Untitled

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1r575JOTRaQgI-HYivLdDmnbcwW_jikWF
"""

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
from torch.nn.utils.rnn import pad_sequence

import nltk
nltk.download('punkt')

import pandas as pd
import numpy as np

from nltk.translate.bleu_score import corpus_bleu, SmoothingFunction

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
smooth = SmoothingFunction().method1

# Cell 2: Load Cleaned DataFrame & Train/Validation Split
# —————————————————————————————————————————————
from sklearn.model_selection import train_test_split

# Add on_bad_lines='skip' to skip lines with parsing errors
df = pd.read_csv("pradyot3.tsv", sep="\t", names=["asm","eng"], on_bad_lines='skip')
print(f"Total examples: {len(df)}")

# Split using scikit-learn
train_df, val_df = train_test_split(df, test_size=0.2, random_state=42)
print(f"Train size: {len(train_df)}, Val size: {len(val_df)}")

# Cell 3: Tokenization & Vocabulary Building
# —————————————————————————————————————
from collections import Counter

def tokenize(text):
    # Ensure text is a string before stripping
    return str(text).strip().split()

# Build counters
ctr_asm, ctr_eng = Counter(), Counter()
# Convert columns to string type to handle potential non-string values (like NaNs)
for sent in train_df["asm"].astype(str):
    ctr_asm.update(tokenize(sent))
for sent in train_df["eng"].astype(str):
    ctr_eng.update(tokenize(sent))

# Special tokens
PAD, SOS, EOS, UNK = "<pad>", "<sos>", "<eos>", "<unk>"
def build_vocab(counter, min_freq=2):
    itos = [PAD, SOS, EOS, UNK] + [w for w,c in counter.items() if c >= min_freq]
    stoi = {tok:i for i,tok in enumerate(itos)}
    return stoi, itos

asm2idx, idx2asm = build_vocab(ctr_asm)
eng2idx, idx2eng = build_vocab(ctr_eng)

print(f"Assamese vocab size: {len(idx2asm)}, English vocab size: {len(idx2eng)}")

# Data validation and statistics
print("Dataset validation:")
print(f"Total examples: {len(df)}")
print(f"Train examples: {len(train_df)}")
print(f"Validation examples: {len(val_df)}")

# Check for empty or very short sentences
train_lengths = []
for _, row in train_df.iterrows():
    asm_len = len(tokenize(row['asm']))
    eng_len = len(tokenize(row['eng']))
    train_lengths.append([asm_len, eng_len])

asm_lens, eng_lens = zip(*train_lengths)
print(f"Average Assamese length: {np.mean(asm_lens):.1f}")
print(f"Average English length: {np.mean(eng_lens):.1f}")

# Check for very short sentences that might cause issues
short_sentences = sum(1 for a, e in train_lengths if a < 2 or e < 2)
print(f"Very short sentences (< 2 tokens): {short_sentences}")

# Cell 4: Functional Dataset + Collate fn
# —————————————————————

# Updated hyperparameters for 10k dataset
HYP = {
    "emb_dim": 256,  # Reduced from 512 to prevent overfitting
    "hid_dim": 512,
    "n_layers": 2,
    "dropout": 0.3,  # Increased dropout for regularization
    "lr": 1e-3,
    "epochs": 30,    # Increased epochs for smaller dataset
    "beam_width": 5,
    "batch_size": 64,  # Reduced batch size for stability
    "clip_grad": 1.0
}

# Functional dataset creation
def create_dataset(df, src2idx, trg2idx, max_len=50):
    """Create dataset as lists of tensors instead of using a class"""
    src_data = []
    trg_data = []

    for i in range(len(df)):
        src_tokens = ["<sos>"] + tokenize(df.iloc[i]["asm"]) + ["<eos>"]
        trg_tokens = ["<sos>"] + tokenize(df.iloc[i]["eng"]) + ["<eos>"]

        # Numericalize, map UNK if missing
        src_ids = [src2idx.get(w, src2idx["<unk>"]) for w in src_tokens][:max_len]
        trg_ids = [trg2idx.get(w, trg2idx["<unk>"]) for w in trg_tokens][:max_len]

        src_data.append(torch.tensor(src_ids))
        trg_data.append(torch.tensor(trg_ids))

    return src_data, trg_data

def collate_fn(batch):
    src_batch, trg_batch = zip(*batch)
    src_padded = pad_sequence(src_batch, padding_value=asm2idx["<pad>"], batch_first=True)
    trg_padded = pad_sequence(trg_batch, padding_value=eng2idx["<pad>"], batch_first=True)
    return src_padded, trg_padded

# Create functional datasets
train_src, train_trg = create_dataset(train_df, asm2idx, eng2idx)
val_src, val_trg = create_dataset(val_df, asm2idx, eng2idx)

# Create datasets as lists of tuples
train_dataset = list(zip(train_src, train_trg))
val_dataset = list(zip(val_src, val_trg))

# Create DataLoaders
train_loader = DataLoader(train_dataset, batch_size=HYP["batch_size"], shuffle=True, collate_fn=collate_fn)
val_loader = DataLoader(val_dataset, batch_size=HYP["batch_size"], shuffle=False, collate_fn=collate_fn)

print(f"Train dataset size: {len(train_dataset)}")
print(f"Validation dataset size: {len(val_dataset)}")

# Cell: Functional Model Implementation
import torch.nn.functional as F

# Create model components as separate modules
def create_encoder(input_dim, emb_dim, hid_dim, n_layers=2, dropout=0.2):
    encoder = nn.ModuleDict({
        'embedding': nn.Embedding(input_dim, emb_dim, padding_idx=asm2idx['<pad>']),
        'dropout': nn.Dropout(dropout),
        'lstm': nn.LSTM(emb_dim, hid_dim, num_layers=n_layers,
                       bidirectional=True, batch_first=True,
                       dropout=dropout if n_layers > 1 else 0)
    })
    encoder.hid_dim = hid_dim
    encoder.n_layers = n_layers
    return encoder

def create_attention(enc_hid, dec_hid):
    attention = nn.ModuleDict({
        'attn': nn.Linear(enc_hid*2 + dec_hid, dec_hid),
        'v': nn.Linear(dec_hid, 1, bias=False)
    })
    return attention

def create_decoder(output_dim, emb_dim, enc_hid, dec_hid, n_layers=2, dropout=0.2):
    decoder = nn.ModuleDict({
        'embedding': nn.Embedding(output_dim, emb_dim, padding_idx=eng2idx['<pad>']),
        'dropout': nn.Dropout(dropout),
        'lstm': nn.LSTM(emb_dim + enc_hid*2, dec_hid, num_layers=n_layers,
                       batch_first=True, dropout=dropout if n_layers > 1 else 0),
        'layer_norm': nn.LayerNorm(dec_hid + enc_hid*2 + emb_dim),
        'fc': nn.Linear(dec_hid + enc_hid*2 + emb_dim, output_dim),
        'dropout_out': nn.Dropout(dropout)
    })
    return decoder

# Forward functions
def encoder_forward(encoder, src):
    # src: [batch, src_len]
    emb = encoder['dropout'](encoder['embedding'](src))  # [batch, src_len, emb_dim]
    outputs, (hidden, cell) = encoder['lstm'](emb)  # outputs: [batch, src_len, hid*2]

    # Combine forward and backward for each layer
    hidden = hidden.view(encoder.n_layers, 2, -1, encoder.hid_dim)
    hidden = torch.cat([hidden[:, 0], hidden[:, 1]], dim=2)

    cell = cell.view(encoder.n_layers, 2, -1, encoder.hid_dim)
    cell = torch.cat([cell[:, 0], cell[:, 1]], dim=2)

    return outputs, (hidden, cell)

def attention_forward(attention, hidden, enc_outputs, src_mask=None):
    batch_size, src_len, _ = enc_outputs.size()
    h = hidden[-1].unsqueeze(1).repeat(1, src_len, 1)  # Use last layer

    energy = torch.tanh(attention['attn'](torch.cat((h, enc_outputs), dim=2)))
    attn = attention['v'](energy).squeeze(2)  # [batch, src_len]

    # Apply mask if provided
    if src_mask is not None:
        attn = attn.masked_fill(src_mask == 0, -1e10)

    return F.softmax(attn, dim=1)

def decoder_forward(decoder, attention, inp, hidden_cell, enc_outputs, src_mask=None):
    hidden, cell = hidden_cell
    emb = decoder['dropout'](decoder['embedding'](inp)).unsqueeze(1)  # [batch, 1, emb_dim]

    # Attention
    a = attention_forward(attention, hidden, enc_outputs, src_mask).unsqueeze(1)  # [batch, 1, src_len]
    weighted = torch.bmm(a, enc_outputs)  # [batch, 1, enc_hid*2]

    # LSTM input
    rnn_in = torch.cat((emb, weighted), dim=2)
    out, (hidden, cell) = decoder['lstm'](rnn_in, (hidden, cell))

    # Prepare output
    out = out.squeeze(1)       # [batch, dec_hid]
    weighted = weighted.squeeze(1)  # [batch, enc_hid*2]
    emb = emb.squeeze(1)       # [batch, emb_dim]

    # Layer norm + residual-like connection
    concat_out = torch.cat((out, weighted, emb), dim=1)
    concat_out = decoder['layer_norm'](concat_out)
    pred = decoder['fc'](decoder['dropout_out'](concat_out))

    return pred, (hidden, cell)

def create_mask(src):
    return (src != asm2idx['<pad>']).float()

def seq2seq_forward(encoder, decoder, attention, src, trg, teacher_forcing_ratio=0.9):
    batch_size, trg_len = trg.size()
    vocab_size = len(idx2eng)

    # Create source mask
    src_mask = create_mask(src)

    # Encode
    enc_out, hidden_cell = encoder_forward(encoder, src)

    # Initialize outputs
    outputs = torch.zeros(batch_size, trg_len, vocab_size).to(src.device)
    input_tok = trg[:, 0]

    for t in range(1, trg_len):
        pred, hidden_cell = decoder_forward(decoder, attention, input_tok, hidden_cell, enc_out, src_mask)
        outputs[:, t] = pred

        # Teacher forcing with probability
        use_teacher_forcing = torch.rand(1).item() < teacher_forcing_ratio
        if use_teacher_forcing:
            input_tok = trg[:, t]
        else:
            input_tok = pred.argmax(1)

    return outputs

# Create model components
encoder = create_encoder(len(idx2asm), HYP["emb_dim"], HYP["hid_dim"],
                        HYP["n_layers"], HYP["dropout"]).to(device)
attention = create_attention(HYP["hid_dim"], HYP["hid_dim"] * 2).to(device)
decoder = create_decoder(len(idx2eng), HYP["emb_dim"], HYP["hid_dim"],
                        HYP["hid_dim"] * 2, HYP["n_layers"], HYP["dropout"]).to(device)

# Count parameters
def count_parameters(encoder, decoder, attention):
    enc_params = sum(p.numel() for p in encoder.parameters() if p.requires_grad)
    dec_params = sum(p.numel() for p in decoder.parameters() if p.requires_grad)
    att_params = sum(p.numel() for p in attention.parameters() if p.requires_grad)
    return enc_params + dec_params + att_params

print(f"Model parameters: {count_parameters(encoder, decoder, attention):,}")

# Functional Beam Search
def beam_search_functional(encoder, decoder, attention, src_sentence, beam_width=HYP["beam_width"], max_len=50, length_penalty=0.6):
    encoder.eval()
    decoder.eval()
    attention.eval()

    with torch.no_grad():
        tokens = ["<sos>"] + src_sentence.split() + ["<eos>"]
        src_ids = [asm2idx.get(w, asm2idx["<unk>"]) for w in tokens]
        src_tensor = torch.tensor(src_ids).unsqueeze(0).to(device)

        # Create source mask
        src_mask = create_mask(src_tensor)

        # Encode
        enc_out, hidden_cell = encoder_forward(encoder, src_tensor)

        # Initialize beams: (score, sequence, hidden_cell_state)
        beams = [(0.0, [eng2idx["<sos>"]], hidden_cell)]
        completed = []

        for step in range(max_len):
            new_beams = []

            for score, seq, hc in beams:
                if len(seq) > 0 and seq[-1] == eng2idx["<eos>"]:
                    completed.append((score, seq))
                    continue

                last_token = torch.tensor([seq[-1]]).to(device)
                pred, hc_new = decoder_forward(decoder, attention, last_token, hc, enc_out, src_mask)

                log_probs = F.log_softmax(pred, dim=1).squeeze(0)
                topv, topi = log_probs.topk(beam_width)

                for i in range(beam_width):
                    token_id = topi[i].item()
                    token_score = topv[i].item()
                    new_seq = seq + [token_id]
                    new_score = score + token_score

                    new_beams.append((new_score, new_seq, hc_new))

            # Keep top beam_width beams
            beams = sorted(new_beams, key=lambda x: x[0] / (len(x[1]) ** length_penalty), reverse=True)[:beam_width]

            if not beams:
                break

        # Add remaining beams to completed
        for score, seq, _ in beams:
            completed.append((score, seq))

        if not completed:
            return []

        # Select best sequence with length normalization
        best_seq = max(completed, key=lambda x: x[0] / (len(x[1]) ** length_penalty))[1]

        # Convert to words and remove special tokens
        result = []
        for token_id in best_seq[1:]:  # Skip <sos>
            if token_id == eng2idx["<eos>"]:
                break
            result.append(idx2eng[token_id])

        return result


def evaluate_functional(encoder, decoder, attention, loader, max_samples=500):
    """Evaluate with sampling to avoid memory issues"""
    encoder.eval()
    decoder.eval()
    attention.eval()
    refs, hyps = [], []

    sample_count = 0
    with torch.no_grad():
        for src, trg in loader:
            for i in range(src.size(0)):
                if sample_count >= max_samples:
                    break

                # Get source sequence
                src_seq = [idx2asm[t.item()] for t in src[i]
                          if t.item() not in {asm2idx["<pad>"], asm2idx["<sos>"], asm2idx["<eos>"]}]

                # Get reference sequence
                trg_seq = [idx2eng[t.item()] for t in trg[i]
                          if t.item() not in {eng2idx["<pad>"], eng2idx["<sos>"], eng2idx["<eos>"]}]

                # Skip empty sequences
                if not src_seq or not trg_seq:
                    continue

                # Get prediction
                pred_seq = beam_search_functional(encoder, decoder, attention, " ".join(src_seq))

                refs.append([trg_seq])
                hyps.append(pred_seq if pred_seq else ["<unk>"])  # Handle empty predictions

                sample_count += 1

            if sample_count >= max_samples:
                break

    if not refs or not hyps:
        return 0.0

    return corpus_bleu(refs, hyps, smoothing_function=smooth)

# Functional Training Loop
from torch.optim.lr_scheduler import ReduceLROnPlateau
import torch.nn.utils as utils

# Create all parameters list for optimizer
all_params = list(encoder.parameters()) + list(decoder.parameters()) + list(attention.parameters())
optimizer = optim.Adam(all_params, lr=HYP["lr"], weight_decay=1e-5)
scheduler = ReduceLROnPlateau(optimizer, mode='max', factor=0.5, patience=3, verbose=True)

# Replace the label_smoothing_loss function in the training cell
def label_smoothing_loss(pred, target, vocab_size, smoothing=0.1, ignore_index=0):
    # pred: [batch_size, vocab_size]
    # target: [batch_size]

    # Create mask for valid tokens (not padding)
    mask = (target != ignore_index)

    # Filter out padding tokens
    pred = pred[mask]
    target = target[mask]

    if pred.size(0) == 0:  # No valid tokens
        return torch.tensor(0.0, device=pred.device, requires_grad=True)

    log_prob = F.log_softmax(pred, dim=1)

    # Create smoothed target distribution
    smooth_target = torch.zeros_like(log_prob)
    smooth_target.fill_(smoothing / (vocab_size - 1))

    # Set correct class probability
    smooth_target.scatter_(1, target.unsqueeze(1), 1.0 - smoothing)

    loss = -torch.sum(log_prob * smooth_target, dim=1)
    return loss.mean()

# Training variables
# Removed early stopping variables: best_bleu, patience_counter, early_stop_patience, epochs_since_best

print("Starting functional training...")
for epoch in range(1, HYP["epochs"] + 1):
    encoder.train()
    decoder.train()
    attention.train()
    epoch_loss = 0
    num_batches = len(train_loader)

    for batch_idx, (src, trg) in enumerate(train_loader):
        src, trg = src.to(device), trg.to(device)

        optimizer.zero_grad()

        # Dynamic teacher forcing ratio
        teacher_forcing_ratio = max(0.5, 1.0 - (epoch - 1) * 0.02)

        output = seq2seq_forward(encoder, decoder, attention, src, trg, teacher_forcing_ratio)

        # Reshape for loss calculation
        output = output[:, 1:].reshape(-1, len(idx2eng))
        trg_y = trg[:, 1:].reshape(-1)

        loss = label_smoothing_loss(output, trg_y, len(idx2eng), smoothing=0.1, ignore_index=eng2idx["<pad>"])
        loss.backward()

        # Gradient clipping
        utils.clip_grad_norm_(all_params, HYP["clip_grad"])

        optimizer.step()
        epoch_loss += loss.item()

        # Print progress every 100 batches
        if batch_idx % 50 == 0:
            print(f'Epoch {epoch}, Batch {batch_idx}/{num_batches}, Loss: {loss.item():.4f}')

    # Validation
    print("Evaluating...")
    val_bleu = evaluate_functional(encoder, decoder, attention, val_loader)
    avg_train_loss = epoch_loss / len(train_loader)

    # Learning rate scheduling
    scheduler.step(val_bleu)
    current_lr = optimizer.param_groups[0]['lr']

    print(f"Epoch {epoch:2d} | Train Loss: {avg_train_loss:.3f} | Val BLEU: {val_bleu*100:.2f} | LR: {current_lr:.6f}")


