from data.iwslt14_small.dataset_utils import create_dataset, BOS_TOKEN, EOS_TOKEN, PAD_TOKEN
from model.lstm2d import LSTM2d
from util.checkpoint_utils import save_checkpoint
from data.data_utils import get_bucket_iterator
import argparse
import torch
import numpy as np
import os
from tensorboardX import SummaryWriter
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.abspath(os.path.join(__file__, os.pardir)))
CHECKPOINT_DIR = ROOT_DIR + '/checkpoints'

# define options
parser = argparse.ArgumentParser(description='train_iwslt14_small.py')
parser.add_argument('--batch_size', type=int, default=32,
                    help='The batch size to use for training and inference.')
parser.add_argument('--epochs', type=int, default=20,
                    help='The number of epochs to train.')
parser.add_argument('--shuffle', type=bool, default=True,
                    help='Whether or not to shuffle the training examples.')
parser.add_argument('--lr', type=float, default=0.0005,
                    help='The learning rate to use.')
parser.add_argument('--embed_dim', type=int, default=128,
                    help='The dimension of the embedding vectors for both the source and target language.')
parser.add_argument('--encoder_state_dim', type=int, default=64,
                    help='The dimension of the bidirectional encoder LSTM states.')
parser.add_argument('--state_2d_dim', type=int, default=128,
                    help='The dimension of the 2D-LSTM hidden & cell states.')
parser.add_argument('--disable_cuda', default=False, action='store_true',
                    help='Disable CUDA (i.e. use the CPU for all computations)')
parser.add_argument('--dropout_p', type=float, default=0.2,
                    help='The dropout probability.')
options = parser.parse_args()

# determine the device (CPU or GPU)
options.device = None
if not options.disable_cuda and torch.cuda.is_available():
    options.device = torch.device('cuda')
else:
    options.device = torch.device('cpu')
print('Using device: {}'.format(options.device))

# create a summary writer
experiment_name = 'b{}_lr{}_emb{}_encstate{}_2dstate{}_drop-p{}_@{}'\
    .format(options.batch_size, options.lr, options.embed_dim, options.encoder_state_dim,
            options.state_2d_dim, options.dropout_p, datetime.now())
writer = SummaryWriter(log_dir='runs/{}'.format(experiment_name))


def main():
    dataset = create_dataset()

    src_vocab_size = len(dataset.src.vocab)
    tgt_vocab_size = len(dataset.tgt.vocab)
    bos_token = dataset.tgt.vocab.stoi[BOS_TOKEN]
    eos_token = dataset.tgt.vocab.stoi[EOS_TOKEN]
    pad_token = dataset.tgt.vocab.stoi[PAD_TOKEN]

    model = LSTM2d(
        embed_dim=options.embed_dim,
        state_dim_2d=options.state_2d_dim,
        encoder_state_dim=options.encoder_state_dim,
        input_vocab_size=src_vocab_size,
        output_vocab_size=tgt_vocab_size,
        bos_token=bos_token,
        eos_token=eos_token,
        pad_token=pad_token,
        device=options.device,
        dropout_p=options.dropout_p
    )

    train_iterator = get_bucket_iterator(dataset.train, batch_size=options.batch_size, shuffle=options.shuffle)
    optimizer = torch.optim.Adam(model.parameters(), lr=options.lr)

    batches_per_epoch = len(dataset.train.examples) // options.batch_size
    val_loss_history = []
    global_step = 0
    for epoch in range(options.epochs):
        print('Starting epoch #{}'.format(epoch + 1))

        writer.add_histogram('input_embeddings', model.input_embedding.weight.data, global_step)
        writer.add_histogram('output_embeddings', model.output_embedding.weight.data, global_step)

        if model.input_embedding.weight.grad is not None and model.output_embedding.weight.grad is not None:
            writer.add_histogram('input_embeddings/gradient', model.input_embedding.weight.grad, global_step)
            writer.add_histogram('output_embeddings/gradient', model.output_embedding.weight.grad, global_step)

        if epoch > 0 and not epoch % 5:
            save_checkpoint(model, optimizer, epoch, options)
            test_model(model, dataset)

        model.train()
        loss_history = []

        train_iterator.init_epoch()
        for i, batch in enumerate(train_iterator):
            optimizer.zero_grad()
            x, x_lengths = batch.src
            x_lengths[x_lengths <= 0] = 1  # crashes for values <= 0 (seems to be a bug)
            y = batch.tgt

            y_pred = model.forward(x=x, x_lengths=x_lengths, y=y)

            loss_value = model.loss(y_pred, y)
            loss_history.append(loss_value.item())
            writer.add_scalar('train_loss', loss_value, global_step=epoch*batches_per_epoch + i)

            loss_value.backward()
            optimizer.step()

            if i > 0 and not i % 100:
                avg_loss = np.mean(loss_history)
                print('Average loss after {} batches (in epoch #{}): {}'.format(i, epoch + 1, avg_loss))

        # calculate loss metrics
        train_loss = np.mean(loss_history)
        val_loss = validate_model(model, dataset)
        val_loss_history.append(val_loss)

        global_step = (epoch+1)*batches_per_epoch
        writer.add_scalar('train_loss', train_loss, global_step)
        writer.add_scalar('val_loss', val_loss, global_step)

    finalize()


def finalize():
    writer.export_scalars_to_json("./all_scalars.json")
    writer.close()


def test_model(model, dataset):
    model.eval()

    example_sentence = 'Good morning , how are you ? <eos>'
    tokens = example_sentence.split(' ')
    x = torch.tensor([[dataset.src.vocab.stoi[w] for w in tokens]], dtype=torch.long).t()
    x_lengths = torch.tensor([len(tokens)], dtype=torch.long)
    pred = model.forward(x=x, x_lengths=x_lengths)

    predicted_tokens = list(torch.argmax(pred, dim=-1).view(-1))
    output_sentence = ' '.join([dataset.tgt.vocab.itos[i] for i in predicted_tokens])
    print('translate(\"{}\") ==> \"{}\"'.format(example_sentence, output_sentence))


def validate_model(model, dataset):
    print("Running validation...")
    val_iterator = get_bucket_iterator(dataset.val, batch_size=options.batch_size, shuffle=False)
    loss_history = []

    model.eval()
    for i, batch in enumerate(val_iterator):
        x, x_lengths = batch.src
        y = batch.tgt
        x_lengths[x_lengths <= 0] = 1

        y_pred = model.forward(x=x, x_lengths=x_lengths, y=y)
        loss_value = model.padded_loss(y_pred, y)
        loss_history.append(loss_value.item())

    avg_loss = np.mean(loss_history)
    print("Average loss on validation dataset: {}".format(avg_loss))
    return avg_loss


if __name__ == '__main__':
    main()
