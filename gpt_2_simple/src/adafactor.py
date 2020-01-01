# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/adafactor.py

"""Optimization."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow.compat.v1 as tf
from tensorflow.python.framework import function



class AdafactorOptimizer(tf.compat.v1.train.Optimizer):
  """Optimizer that implements the Adafactor algorithm.

  Adafactor is described in https://arxiv.org/abs/1804.04235.

  Adafactor is most similar to Adam (Kingma and Ba), the major differences are:

  1. For a two-dimensional AxB weight matrix, Adafactor uses only A+B auxiliary
     parameters to maintain the second-moment estimator, instead of AB.
     This is advantageous on memory-limited systems.  In addition, beta1
     (momentum) is set to zero by default, saving an additional auxiliary
     parameter per weight.  Variables with >=3 dimensions are treated as
     collections of two-dimensional matrices - factorization is over the final
     two dimensions.

  2. Adafactor incorporates "update-clipping" - a scale-invariant analog of
     gradient clipping.  This adds stability

  3. Adafactor does not require an external "learning rate".  By default, it
     incorporates a relative-update-scale schedule, corresponding to
     inverse-square-root learning-rate-decay in ADAM.  We hope this works well
     for most applications.

  ALGORITHM:

  parameter -= absolute_update_scale * clip(grad / grad_scale)

  where:

    absolute_update_scale := relative_update_scale * parameter_scale
    relative_update_scale := min((step_num + 1)**-0.5, 1e-2)
    parameter_scale := max(rms(var)), epsilon2)
    clip(x) := x / max(1.0, rms(x))
    grad_scale := tf.sqrt(v)   (v is the second-moment estimator)

  The second-moment estimator v is maintained in a manner similar to Adam:
  We initialize
  ```
  if var is 2-dimensional:
    v_r <- zeros([num_rows])
    v_c <- zeros([num_cols])
  if var is 0-dimensional or 1-dimensional:
    v <- zeros(shape(var))
  ```

  The update rule is as follows:
  ```
  decay_rate = 1 - (step_num + 1) ^ -0.8
  grad_squared = tf.square(grad) + epsilon1
  if var is 2-dimensional:
    v_r <- decay_rate * v_r + (1 - decay_rate) * reduce_mean(grad_squared, 1)
    v_c <- decay_rate * v_c + (1 - decay_rate) * reduce_mean(grad_squared, 0)
    v = outer_prod(v_r, v_c) / reduce_mean(v_r)
  if var is 0-dimensional or 1-dimensional:
    v <- decay_rate * v + (1 - decay_rate) * grad_squared
  ```

  For variables with >=3 dimensions, we factorize the second-moment accumulator
  over the final 2 dimensions.  See the code for details.


  Several parts of this algorithm are configurable from the initializer.

    multiply_by_parameter_scale:  If True, then compute absolute_update_scale
      as described above.  If False, let absolute_update_scale be the externally
      supplied learning_rate.
    learning_rate: represents relative_update_scale if
      multiply_by_parameter_scale==True, or absolute_update_scale if
      multiply_by_parameter_scale==False.
    decay_rate: Decay rate of the second moment estimator (varies by step_num).
      This should be set to a function such that:
      1-1/(step_num + 1) <= decay_rate(step_num) < 1.0
    beta1: enables momentum, as in Adam.  Uses extra memory if nonzero.
    clipping_threshold: should be >=1.0 or None for no update clipping
    factored: whether to factor the second-moment estimator.  True means
      less memory usage.

  """

  def __init__(self,
               multiply_by_parameter_scale=True,
               learning_rate=None,
               decay_rate=None,
               beta1=0.0,
               clipping_threshold=1.0,
               factored=True,
               simulated_quantize_bits=None,
               parameter_encoding=None,
               use_locking=False,
               name="Adafactor",
               epsilon1=1e-30,
               epsilon2=1e-3):
    """Construct a new Adafactor optimizer.

    See class comment.

    Args:
      multiply_by_parameter_scale: a boolean
      learning_rate: an optional Scalar.
      decay_rate: an optional Scalar.
      beta1: a float value between 0 and 1
      clipping_threshold: an optional float >= 1
      factored: a boolean - whether to use factored second-moment estimator
        for 2d variables
      simulated_quantize_bits: train with simulated quantized parameters
        (experimental)
      parameter_encoding: a ParameterEncoding object to use in the case of
        bfloat16 variables.
      use_locking: If True use locks for update operations.
      name: Optional name for the operations created when applying gradients.
        Defaults to "AdafactorOptimizer".
      epsilon1: Regularization constant for squared gradient.
      epsilon2: Regularization constant for parameter scale.

    Raises:
      ValueError: if absolute_update_scale and relative_update_scale_fn are both
        present or both absent.
    """
    super(AdafactorOptimizer, self).__init__(use_locking, name)
    self._multiply_by_parameter_scale = multiply_by_parameter_scale
    if learning_rate is None:
      learning_rate = self._learning_rate_default(multiply_by_parameter_scale)
    self._learning_rate = learning_rate
    if decay_rate is None:
      decay_rate = self._decay_rate_default()
    self._decay_rate = decay_rate
    self._beta1 = beta1
    self._clipping_threshold = clipping_threshold
    self._factored = factored
    self._simulated_quantize_bits = simulated_quantize_bits
    self._parameter_encoding = parameter_encoding
    self._quantization_noise = noise_from_step_num()
    self._epsilon1 = epsilon1
    self._epsilon2 = epsilon2

  def _should_use_factored_second_moment_estimate(self, shape):
    """Should we use a factored second moment estimator.

    Based on the shape of the variable.

    Args:
      shape: a list of integers
    Returns:
      a boolean
    """
    return self._factored and len(shape) >= 2

  def _create_slots(self, var_list):
    for var in var_list:
      shape = var.get_shape().as_list()
      if self._beta1:
        self._zeros_slot(var, "m", self._name)
      if self._should_use_factored_second_moment_estimate(shape):
        r_val = tf.zeros(shape[:-1], dtype=tf.float32)
        c_val = tf.zeros(shape[:-2] + shape[-1:], dtype=tf.float32)
        self._get_or_make_slot(var, r_val, "vr", self._name)
        self._get_or_make_slot(var, c_val, "vc", self._name)
      else:
        v_val = tf.zeros(shape, dtype=tf.float32)
        self._get_or_make_slot(var, v_val, "v", self._name)

  def _apply_dense(self, grad, var):
    return self._resource_apply_dense(grad, var)

  def _apply_sparse(self, grad, var):
    return self._apply_dense(tf.convert_to_tensor(grad), var)

  def _resource_apply_sparse(self, grad, handle, indices):
    return self._resource_apply_dense(
        tf.convert_to_tensor(tf.IndexedSlices(grad, indices, tf.shape(handle))),
        handle)

  def _parameter_scale(self, var):
    """Estimate the scale of the parameters from the current values.

    We include a minimum value of 0.001 to give it a chance to escape 0
    if it was zero-initialized.

    Instead of using the value, we could impute the scale from the shape,
    as initializers do.

    Args:
      var: a variable or Tensor.
    Returns:
      a Scalar
    """
    return tf.maximum(reduce_rms(var), self._epsilon2)

  def _resource_apply_dense(self, grad, handle):
    var = handle
    grad = tf.to_float(grad)
    grad_squared = tf.square(grad) + self._epsilon1
    grad_squared_mean = tf.reduce_mean(grad_squared)
    decay_rate = self._decay_rate
    update_scale = self._learning_rate
    old_val = var
    if var.dtype.base_dtype == tf.bfloat16:
      old_val = tf.to_float(self._parameter_encoding.decode(old_val))
    if self._multiply_by_parameter_scale:
      update_scale *= tf.to_float(self._parameter_scale(old_val))
    # HACK: Make things dependent on grad.
    # This confounds the XLA rewriter and keeps it from fusing computations
    # across different variables.  This fusion is a bad for HBM usage, since
    # it causes the gradients to persist in memory.
    decay_rate += grad_squared_mean * 1e-30
    update_scale += grad_squared_mean * 1e-30
    # END HACK
    mixing_rate = 1.0 - decay_rate
    shape = var.get_shape().as_list()
    updates = []
    if self._should_use_factored_second_moment_estimate(shape):
      grad_squared_row_mean = tf.reduce_mean(grad_squared, -1)
      grad_squared_col_mean = tf.reduce_mean(grad_squared, -2)
      vr = self.get_slot(var, "vr")
      new_vr = (decay_rate * vr + mixing_rate * grad_squared_row_mean)
      vc = self.get_slot(var, "vc")
      new_vc = (decay_rate * vc + mixing_rate * grad_squared_col_mean)
      vr_update = tf.assign(vr, new_vr, use_locking=self._use_locking)
      vc_update = tf.assign(vc, new_vc, use_locking=self._use_locking)
      updates = [vr_update, vc_update]
      long_term_mean = tf.reduce_mean(new_vr, -1, keepdims=True)
      r_factor = tf.rsqrt(new_vr / long_term_mean)
      c_factor = tf.rsqrt(new_vc)
      x = grad * tf.expand_dims(r_factor, -1) * tf.expand_dims(c_factor, -2)
    else:
      v = self.get_slot(var, "v")
      new_v = decay_rate * v + mixing_rate * grad_squared
      v_update = tf.assign(v, new_v, use_locking=self._use_locking)
      updates = [v_update]
      x = grad * tf.rsqrt(new_v)
    if self._clipping_threshold is not None:
      clipping_denom = tf.maximum(1.0, reduce_rms(x) / self._clipping_threshold)
      x /= clipping_denom
    subtrahend = update_scale * x
    if self._beta1:
      m = self.get_slot(var, "m")
      new_m = self._beta1 * tf.to_float(m) + (1.0 - self._beta1) * subtrahend
      subtrahend = new_m
      new_m = cast_like(new_m, var)
      updates.append(tf.assign(m, new_m, use_locking=self._use_locking))
    new_val = tf.to_float(old_val) - subtrahend
    if var.dtype.base_dtype == tf.bfloat16:
      new_val = self._parameter_encoding.encode(
          new_val, self._quantization_noise)
    if self._simulated_quantize_bits:
      new_val = simulated_quantize(
          var - subtrahend, self._simulated_quantize_bits,
          self._quantization_noise)
    var_update = tf.assign(var, new_val, use_locking=self._use_locking)
    updates = [var_update] + updates
    return tf.group(*updates)

  def _decay_rate_default(self):
    return adafactor_decay_rate_pow(0.8)

  def _learning_rate_default(self, multiply_by_parameter_scale):
    learning_rate = tf.minimum(tf.rsqrt(step_num() + 1.0), 0.01)
    if not multiply_by_parameter_scale:
      learning_rate *= 0.05
    return learning_rate


def adafactor_decay_rate_adam(beta2):
  """Second-moment decay rate like Adam, subsuming the correction factor.

  Args:
    beta2: a float between 0 and 1
  Returns:
    a scalar
  """
  t = tf.to_float(tf.train.get_or_create_global_step()) + 1.0
  decay = beta2 * (1.0 - tf.pow(beta2, t - 1.0)) / (1.0 - tf.pow(beta2, t))
  # decay = tf.cond(tf.equal(t, 1.0), lambda: beta2, lambda: decay)
  return decay


def adafactor_decay_rate_pow(exponent):
  """Second moment decay rate where memory-length grows as step_num^exponent.

  Args:
    exponent: a float between 0 and 1
  Returns:
    a scalar
  """
  return 1.0 - tf.pow((step_num() + 1.0), -exponent)


def step_num():
  return tf.to_float(tf.train.get_or_create_global_step())


def adafactor_optimizer_from_hparams(hparams, lr):
  """Create an Adafactor optimizer based on model hparams.

  Args:
    hparams: model hyperparameters
    lr: learning rate scalar.
  Returns:
    an AdafactorOptimizer
  Raises:
    ValueError: on illegal values
  """
  if hparams.optimizer_adafactor_decay_type == "adam":
    decay_rate = adafactor_decay_rate_adam(
        hparams.optimizer_adafactor_beta2)
  elif hparams.optimizer_adafactor_decay_type == "pow":
    decay_rate = adafactor_decay_rate_pow(
        hparams.optimizer_adafactor_memory_exponent)
  else:
    raise ValueError("unknown optimizer_adafactor_decay_type")
  if hparams.weight_dtype == "bfloat16":
    parameter_encoding = EighthPowerEncoding()
  else:
    parameter_encoding = None
  return AdafactorOptimizer(
      multiply_by_parameter_scale=(
          hparams.optimizer_adafactor_multiply_by_parameter_scale),
      learning_rate=lr,
      decay_rate=decay_rate,
      beta1=hparams.optimizer_adafactor_beta1,
      clipping_threshold=hparams.optimizer_adafactor_clipping_threshold,
      factored=hparams.optimizer_adafactor_factored,
      simulated_quantize_bits=getattr(
          hparams, "simulated_parameter_quantize_bits", 0),
      parameter_encoding=parameter_encoding,
      use_locking=False,
      name="Adafactor")


def reduce_rms(x):
  return tf.sqrt(tf.reduce_mean(tf.square(x)))


# https://github.com/tensorflow/tensor2tensor/blob/8107e53/tensor2tensor/layers/common_layers.py#L3454-L3471
def cast_like(x, y):
  """Cast x to y's dtype, if necessary."""
  x = tf.convert_to_tensor(x)
  y = tf.convert_to_tensor(y)

  if x.dtype.base_dtype == y.dtype.base_dtype:
    return x

  cast_x = tf.cast(x, y.dtype)
  if cast_x.device != x.device:
    x_name = "(eager Tensor)"
    try:
      x_name = x.name
    except AttributeError:
      pass
    tf.logging.warning("Cast for %s may induce copy from '%s' to '%s'", x_name,
                       x.device, cast_x.device)
  return cast_x


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L137-L157
def noise_from_step_num():
  """Quantization noise equal to (phi * (step_num + 1)) mod 1.0.
  Not using random_uniform here due to a problem on TPU in that random seeds
  are not respected, which may cause the parameters on different replicas
  to go out-of-sync.
  Returns:
    a float32 scalar
  """
  step = tf.to_int32(tf.train.get_or_create_global_step()) + 1
  phi = ((5 ** 0.5) - 1) / 2
  # Naive computation tf.mod(phi * step, 1.0) in float32 would be disastrous
  # due to loss of precision when the step number gets large.
  # Computation in doubles does not work on TPU, so we use this complicated
  # alternative computation which does not suffer from these roundoff errors.
  ret = 0.0
  for i in range(30):
    ret += (((phi * (2 ** i)) % 1.0)  # double-precision computation in python
            * tf.to_float(tf.mod(step // (2 ** i), 2)))
  return tf.mod(ret, 1.0)


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L89-L134
def simulated_quantize(x, num_bits, noise):
  """Simulate quantization to num_bits bits, with externally-stored scale.
  num_bits is the number of bits used to store each value.
  noise is a float32 Tensor containing values in [0, 1).
  Each value in noise should take different values across
  different steps, approximating a uniform distribution over [0, 1).
  In the case of replicated TPU training, noise should be identical
  across replicas in order to keep the parameters identical across replicas.
  The natural choice for noise would be tf.random_uniform(),
  but this is not possible for TPU, since there is currently no way to seed
  the different cores to produce identical values across replicas.  Instead we
  use noise_from_step_num() (see below).
  The quantization scheme is as follows:
  Compute the maximum absolute value by row (call this max_abs).
  Store this either in an auxiliary variable or in an extra column.
  Divide the parameters by (max_abs / (2^(num_bits-1)-1)).  This gives a
  float32 value in the range [-2^(num_bits-1)-1, 2^(num_bits-1)-1]
  Unbiased randomized roundoff by adding noise and rounding down.
  This produces a signed integer with num_bits bits which can then be stored.
  Args:
    x: a float32 Tensor
    num_bits: an integer between 1 and 22
    noise: a float Tensor broadcastable to the shape of x.
  Returns:
    a float32 Tensor
  """
  shape = x.get_shape().as_list()
  if not (len(shape) >= 2 and shape[-1] > 1):
    return x
  max_abs = tf.reduce_max(tf.abs(x), -1, keepdims=True) + 1e-9
  max_int = 2 ** (num_bits - 1) - 1
  scale = max_abs / max_int
  x /= scale
  x = tf.floor(x + noise)
  # dequantize before storing (since this is a simulation)
  x *= scale
  return x


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L271-L291
class _EncodingInitializer(object):
  """Helper class for ParameterEncoding.
  Initializes variables by calling base initializer, then encoding.
  """

  def __init__(self, base_initializer, parameter_encoding):
    self._base_initializer = base_initializer
    self._parameter_encoding = parameter_encoding

  def __call__(self, shape, dtype, partition_info=None):
    if self._base_initializer is None:
      # mimic default initialization in tf.get_variable()
      if dtype.is_floating:
        ret = tf.glorot_uniform_initializer()(shape, dtype)
      else:
        ret = tf.zeros(shape, dtype)
    else:
      ret = self._base_initializer(shape, dtype, partition_info=partition_info)
    noise = 0.0  # no random noise in the initializer.
    return tf.cast(self._parameter_encoding.encode(ret, noise), dtype)


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L209-L268
class ParameterEncoding(object):
  """Helper class for encoding weights as bfloat16.
  For now, the parameters are always stored (encoded) as bfloat16 and decoded
  to bfloat32.  Confusingly, the custom getter then converts the bfloat32 back
  to a bfloat16 to use as an activation, assuming that we use bfloat16 for
  activations.
  TODO(noam): Add options for activation dtype=float32, and for different
  storage dtypes.
  """

  def encode(self, x, noise):
    """Encode float32 to bfloat16.
    Args:
      x: a float32 Tensor
      noise: a float32 Tensor with values in [0, 1), broadcastable to shape(x)
    Returns:
      a bfloat16 Tensor
    """
    raise NotImplementedError("encode not implemented")

  def decode(self, x):
    """Decode bfloat16 to float32."""
    raise NotImplementedError("decode not implemented")

  def _decode_with_identity_gradient(self, x):
    # identity backprop through the decoder.
    # This means that the optimizer must call encode when updating weights.
    @function.Defun(python_grad_func=lambda op, dy: dy,
                    shape_func=lambda op: [op.inputs[0].get_shape()])
    def my_fn(x):
      return self.decode(x)
    return my_fn(x)

  def custom_getter(self, activation_dtype=tf.bfloat16):
    """A custom getter that uses the encoding for bfloat16 and float32 vars.
    When a bfloat16 or float32 variable is requsted, an encoded float16
    varaible is created, which is then decoded and cast to a bfloat16
    activation.
    Args:
      activation_dtype: a dtype to which to convert the decoded value.
    Returns:
      a function.
    """
    def getter_fn(getter, *args, **kwargs):
      requested_dtype = kwargs["dtype"]
      if requested_dtype in (tf.bfloat16, tf.float32):
        kwargs["dtype"] = tf.bfloat16
        kwargs["initializer"] = _EncodingInitializer(
            kwargs["initializer"], self)
        ret = self._decode_with_identity_gradient(getter(*args, **kwargs))
        return tf.cast(ret, activation_dtype)
      return getter(*args, **kwargs)
    return getter_fn


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L160-L183
def _randomized_roundoff_to_bfloat16(x, noise, cand1, cand2):
  """Round-off x to cand1 or to cand2 in an unbiased way.
  Cand1 and cand2 are the same shape as x.
  For every element of x, the corresponding elements of cand1 and cand2 should
  be the two closest bfloat16 values to x.  Order does not matter.
  cand1 and cand2 must differ from each other.
  Args:
    x: A float32 Tensor.
    noise: A Tensor broadcastable to the shape of x containing
    random uniform values in [0.0, 1.0].
    cand1: A bfloat16 Tensor the same shape as x.
    cand2: A bfloat16 Tensor the same shape as x.
  Returns:
    A bfloat16 Tensor.
  """
  cand1_f = tf.to_float(cand1)
  cand2_f = tf.to_float(cand2)
  step_size = cand2_f - cand1_f
  fpart = (x - cand1_f) / step_size
  ret = tf.where(tf.greater(fpart, noise), cand2, cand1)
  return ret


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L186-L206
def _to_bfloat16_unbiased(x, noise):
  """Convert a float32 to a bfloat16 using randomized roundoff.
  Args:
    x: A float32 Tensor.
    noise: a float32 Tensor with values in [0, 1), broadcastable to tf.shape(x)
  Returns:
    A float32 Tensor.
  """
  x_sign = tf.sign(x)
  # Make sure x is positive.  If it is zero, the two candidates are identical.
  x = x * x_sign + 1e-30
  cand1 = tf.to_bfloat16(x)
  cand1_f = tf.to_float(cand1)
  # This relies on the fact that for a positive bfloat16 b,
  # b * 1.005 gives you the next higher bfloat16 and b*0.995 gives you the
  # next lower one. Both 1.005 and 0.995 are ballpark estimation.
  cand2 = tf.to_bfloat16(
      tf.where(tf.greater(x, cand1_f), cand1_f * 1.005, cand1_f * 0.995))
  ret = _randomized_roundoff_to_bfloat16(x, noise, cand1, cand2)
  return ret * tf.to_bfloat16(x_sign)


# https://github.com/tensorflow/tensor2tensor/blob/8a084a4/tensor2tensor/utils/quantization.py#L294-L314
class EighthPowerEncoding(ParameterEncoding):
  """enc(x) = sign(x) * (abs(x)*128)^8.
  This provides less range and more resolution.
  The range of representable positive values is approximately [2^-23, 2^9]
  Resolution is 8x better than bfloat16.
  """

  def encode(self, x, noise):
    x = tf.to_float(x)
    # we can't use tf.pow(..., 8.0) because of a high-error approximation
    # on TPU.  Instead we square three times.
    x = tf.sign(x) * tf.square(tf.square(tf.square(tf.abs(x) * 128.0)))
    x = _to_bfloat16_unbiased(x, noise)
    return x

  def decode(self, x):
    x = tf.to_float(x)
    # we can't use tf.pow(..., 0.125) because of a high-error approximation
    # on TPU.  Instead we sqrt three times.
    return tf.sign(x) * (tf.sqrt(tf.sqrt(tf.sqrt(tf.abs(x)))) / 128.0)
