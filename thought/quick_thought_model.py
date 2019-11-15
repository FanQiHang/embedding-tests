from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
from transformer_layers import TransformerBlock


def _initialize_cell(num_units, name, cell_type="GRU"):
  if cell_type == "GRU":
    cell = tf.contrib.rnn.GRUCell(num_units=num_units, name=name)
  elif cell_type == "LSTM":
    cell = tf.contrib.rnn.LSTMCell(num_units=num_units, name=name)
  else:
    raise ValueError("Invalid cell type")
  return cell


def _initialze_dense(num_units, name, activation=None):
  dense = tf.keras.layers.Dense(num_units, activation=activation, name=name,
                                use_bias=False)
  return dense


def _initialize_cudurnn(num_units, name, cell_type="GRU", go_backwards=False,
                        return_sequences=False):
  if cell_type == "GRU":
    cell = tf.keras.layers.CuDNNGRU(units=num_units, name=name,
                                    return_sequences=return_sequences,
                                    go_backwards=go_backwards)
  elif cell_type == "LSTM":
    cell = tf.keras.layers.CuDNNLSTM(units=num_units, name=name,
                                     return_sequences=return_sequences,
                                     go_backwards=go_backwards)
  else:
    raise ValueError("Invalid cell type")
  return cell


def _get_angles(pos, i, d_model):
  angle_rates = 1 / np.power(10000, (2 * (i//2)) / np.float32(d_model))
  return pos * angle_rates


def _positional_encoding(position, d_model):
  angle_rads = _get_angles(np.arange(position)[:, np.newaxis],
                           np.arange(d_model)[np.newaxis, :],
                           d_model)
  # apply sin to even indices in the array; 2i
  angle_rads[:, 0::2] = np.sin(angle_rads[:, 0::2])
  # apply cos to odd indices in the array; 2i+1
  angle_rads[:, 1::2] = np.cos(angle_rads[:, 1::2])
  pos_encoding = angle_rads[np.newaxis, ...]
  return tf.cast(pos_encoding, dtype=tf.float32)


class QuickThoughtModel(object):
  def __init__(self, vocab_size, emb_dim, encoder_dim, context_size,
               cell_type="TRANS", num_layer=2, init_word_emb=None,
               train=True, drop_p=0.):

    super(QuickThoughtModel, self).__init__()
    self.context_size = context_size
    self.encoder_dim = encoder_dim
    self.vocab_size = vocab_size
    self.emb_dim = emb_dim
    self.cell_type = cell_type
    self.train = train
    self.thought_vector = None
    self.drop_p = drop_p

    if self.encoder_dim % 2:
      raise ValueError(
        "encoder_dim must be even when using a bidirectional encoder.")

    self.word_in_emb = tf.Variable(self.get_or_init_word_emb(init_word_emb),
                                   name="emb_in")
    self.word_out_emb = tf.Variable(self.get_or_init_word_emb(init_word_emb),
                                    name="emb_out")
    self.proj_in = _initialze_dense(encoder_dim, "proj_in")

    if cell_type in ["GRU", "LSTM"]:
      self.in_cell_fw = _initialize_cudurnn(encoder_dim // 2, "rnn_in_fw",
                                            cell_type, False, True)
      self.in_cell_bw = _initialize_cudurnn(encoder_dim // 2, "rnn_in_bw",
                                            cell_type, False, True)
      self.in_cells = [self.in_cell_fw, self.in_cell_bw]
      self.out_cell_fw = _initialize_cudurnn(encoder_dim // 2, "rnn_out_fw",
                                             cell_type, False, True)
      self.out_cell_bw = _initialize_cudurnn(encoder_dim // 2, "rnn_out_bw",
                                             cell_type, False, True)
      self.out_cells = [self.out_cell_fw, self.out_cell_bw]
      # no projection as in original paper
      self.proj_in = None  # _initialze_dense(encoder_dim, "proj_in")
      self.proj_out = None  # _initialze_dense(encoder_dim, "proj_out")
    else:
      assert cell_type == "TRANS"
      self.pos_encoding = _positional_encoding(4096, self.emb_dim)
      with tf.variable_scope("in_cell"):
        self.in_cell = TransformerBlock(encoder_dim, encoder_dim * 4,
                                        train=train, num_layer=num_layer)
      self.in_cells = [self.in_cell]
      with tf.variable_scope("out_cell"):
        self.out_cell = TransformerBlock(encoder_dim, encoder_dim * 4,
                                         train=train, num_layer=num_layer)
      self.out_cells = [self.out_cell]

      self.proj_in = _initialze_dense(encoder_dim, "proj_in")
      self.proj_out = _initialze_dense(encoder_dim, "proj_out")

  def get_or_init_word_emb(self, weights=None):
    if weights is None:
      return tf.random.normal((self.vocab_size, self.emb_dim), 0.,
                              self.emb_dim ** -0.5)
    else:
      return tf.convert_to_tensor(weights, dtype=tf.float32)

  def encode(self, inputs, masks, cells, proj):
    masks = tf.cast(masks, dtype=tf.int32)
    if self.cell_type in ["GRU", "LSTM"]:
      state = self.encode_cudarnn(inputs, masks, cells[0], cells[1])
    else:
      seq_len = tf.shape(inputs)[1]
      inputs += self.pos_encoding[:, :seq_len, :]
      state = cells[0].forward(inputs, masks)
      state = proj(state)

    if self.train and self.drop_p > 0.:
      state = tf.nn.dropout(state, rate=self.drop_p)

    return state

  def encode_cudarnn(self, inputs, masks, cell_fw, cell_bw):
    assert self.cell_type in ["GRU", "LSTM"]
    inputs = tf.multiply(inputs, tf.cast(tf.expand_dims(masks, 2), tf.float32))
    indices = tf.reduce_sum(masks, 1, keepdims=True) - 1

    # forward pass
    outputs_fw = cell_fw(inputs)
    state_fw = tf.gather_nd(outputs_fw, indices, batch_dims=1)
    # reverse and backward pass
    inputs_bw = tf.reverse_sequence(inputs, tf.reduce_sum(masks, 1), 1, 0)
    outputs_bw = cell_bw(inputs_bw)
    state_bw = tf.gather_nd(outputs_bw, indices, batch_dims=1)

    state = tf.concat([state_fw, state_bw], 1)
    return state

  def forward_triplet(self, inputs, outputs, batch_size):
    encode_in_emb = tf.nn.embedding_lookup(self.word_in_emb, inputs[0])
    thought_in_vectors = self.encode(encode_in_emb, inputs[1],
                                     self.in_cells, self.proj_in)
    self.thought_vector = thought_in_vectors
    targets = tf.range(batch_size, dtype=tf.int64)
    losses = 0
    accs = []
    for tup in outputs:
      encode_out_emb = tf.nn.embedding_lookup(self.word_out_emb, tup[0])
      thought_out_vectors = self.encode(encode_out_emb, tup[1],
                                        self.out_cells, self.proj_out)
      logits = tf.matmul(thought_in_vectors, thought_out_vectors,
                         transpose_b=True)
      losses += tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          labels=targets, logits=logits))

      acc = tf.cast(tf.equal(targets, tf.argmax(logits, axis=1)),
                    dtype=tf.float32)
      accs.append(tf.reduce_mean(acc))

    loss = tf.reduce_mean(losses)
    return accs, loss

  @staticmethod
  def eval_acc(logits):
    batch_size = tf.cast(tf.shape(logits)[0], dtype=np.int64)
    f_scores, b_scores = logits[:-1], logits[1:]
    bw_targets = tf.range(batch_size - 1, dtype=tf.int64)
    fwd_targets = bw_targets + 1

    fw_acc = tf.cast(tf.equal(fwd_targets, tf.argmax(f_scores, axis=1)),
                     dtype=tf.float32)
    bw_acc = tf.cast(tf.equal(bw_targets, tf.argmax(b_scores, axis=1)),
                     dtype=tf.float32)
    return tf.reduce_mean(fw_acc), tf.reduce_mean(bw_acc)


