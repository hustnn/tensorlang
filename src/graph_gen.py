import sys

import tensorflow as tf

def eprint(*args, **kwargs):
  print(*args, file=sys.stderr, **kwargs)

class RetvalBag:
  def __init__(self, a_dict):
    self._d = {}

    for k, v in a_dict.items():
      if type(v) == RetvalBag:
        raise "Can't put a RetvalBag into another. %s" % a_dict
      self._d[k] = v

  def get(self, key):
    if key == None:
      l = len(self._d)
      if l == 0:
        raise "Can't get default retval for an empty RetvalBag"
      if l > 1:
        raise "Can't get default retval a RetvalBag with more than one entry: %s" % self._d
      key = list(self._d.keys())[0]

    return self._d[key]

  def length(self):
    return len(self._d)


class PrimitiveFunction:
  def __init__(self, fn):
    self._fn = fn

  def apply(self, visitor, name, kwargs, args):
    if kwargs == None:
      kwargs = {}

    if name != None:
      kwargs = dict(kwargs)
      kwargs['name'] = name

    try:
      return self._fn(*args, **kwargs)
    except:
      raise Exception("Tried to call %s with args %s and kwargs %s"  % (self._fn, args, kwargs))

class SyntheticFunction:
  def __init__(self, argnames, retnames, graphdef):
    self._argnames = argnames
    self._retnames = retnames
    self._graphdef = graphdef

  def apply(self, visitor, scope_name, kwargs, args):
    retvals = tf.import_graph_def(
      self._graphdef,
      name=scope_name,
      input_map=dict(zip(self._argnames, args)),
      return_elements=self._retnames)

    return RetvalBag(dict(zip(self._retnames, retvals)))

class Nao:
  def reasm(self, argvars, retvals, name=None):
    a = [argvar.name for argvar in argvars]
    r = [retval.name for retval in retvals]
    graph = argvars[0].graph

    return SyntheticFunction(a, r, graph.as_graph_def())

  def disasm(self, fn, name=None):
    argvars, retvals = fn.disasm()
    return RetvalBag({"inputs": argvars, "outputs": retvals})

class DeclaredFunction:
  def __init__(self, ctx, expr):
    self._ctx = ctx
    self._expr = expr

  def _name(self):
    return self._expr[0]

  def _attr_specs(self):
    return self._expr[1];

  def _arg_specs(self):
    return self._expr[2]

  def _arg_names(self):
    return [name for (name, shape, dtype) in self._arg_specs()]

  def _retval_specs(self):
    return self._expr[3]

  def _retval_argnames(self):
    return [name for (_, name) in self._retval_specs()]

  def _body(self):
    return self._expr[4:]

  def get(self, key):
    if key == "outputs":
      return list(self._gen_cached()[1]._d.values())

    if key == "inputs":
      return self._gen_cached()[0]

    raise "Unknown key for function %s" % key

  def disasm(self):
    with tf.Graph().as_default() as g:
      # TODO(adamb) Should actually have captured the environment where the function was defined.
      visitor = TopLevel()
      new_ctx = self._ctx.subcontext()

      arg_vars = []
      for (arg_name, shape_expr, dtype_expr) in self._arg_specs():
        arg_var = tf.placeholder(
          name=arg_name,
          dtype=visitor.visit(new_ctx, dtype_expr),
          shape=visitor.visit(new_ctx, shape_expr),
        )
        arg_vars.append(arg_var)
        new_ctx.define_local(arg_name, arg_var)

      for expr in self._body():
        visitor.visit(new_ctx, expr)

      retvals = [new_ctx.get_local(retval_argname) for retval_argname in self._retval_argnames()]
      return (arg_vars, retvals)

  def apply(self, visitor, scope_name, attrs, args):
    returned = {}
    new_ctx = self._ctx.subcontext()
    if attrs != None:
      for name, value in attrs.items():
        new_ctx.define_attr(name, value)

    with tf.variable_scope(scope_name):
      # preload locals with references to input operations
      for arg, arg_name in zip(args, self._arg_names()):
        new_ctx.define_local(arg_name, arg)

      # Need to visit expressions
      for expr in self._body():
        visitor.visit(new_ctx, expr)

    for retval_name, retval_argname in self._retval_specs():
      returned[retval_name] = new_ctx.get_local(retval_argname)

    # For now we only use the first retval
    return RetvalBag(returned)


class Context:
  def __init__(self, global_namespaces, parent=None):
    self._parent = parent
    self._functions = {}
    self._namespaces = global_namespaces
    self._attrs = {}
    self._locals = {}
    self._root_suffixes = {}
    self._leaves = set()

  def subcontext(self):
    return Context(self._namespaces, parent=self)

  def namespace_lookup(self, ns_name, key):
    if ns_name in self._namespaces:
      ns = self._namespaces[ns_name]
      return PrimitiveFunction(getattr(ns, key));

    if self._parent:
      return self._parent.namespace_lookup(ns_name, key)

  def set_function(self, name, defn):
    self._functions[name] = DeclaredFunction(self, [name, *defn])

  def define_local(self, name, value):
    if name in self._locals:
      raise Exception("Local already defined: %s" % name)
    self._locals[name] = value

  def define_attr(self, name, value):
    if name in self._attrs:
      raise Exception("Attribute already defined: %s" % name)

    if name in self._locals:
      raise Exception("Can't define attribute. Local exists with name: %s" % name)

    self._attrs[name] = value

  def get_local(self, name):
    if name in self._locals:
      return self._locals[name]

    if name in self._attrs:
      return self._attrs[name]

    if name in self._functions:
      return self._functions[name]

    if self._parent:
      return self._parent.get_local(name)

    raise Exception("No such local or function: %s. Have: %s" % (name, self._locals))

  def get_attr(self, name):
    if name in self._attrs:
      return self._attrs[name]

    raise Exception("No such attribute: %s" % name)

  def possible_leaf(self, op):
    t = type(op)
    if t == tf.Tensor or t == tf.Operation:
      self._leaves.add(op)

  def eliminate_leaf(self, op):
    t = type(op)
    if t == tf.Tensor or t == tf.Operation:
      self._leaves.discard(op)

    if self._parent:
      return self._parent.eliminate_leaf(op)

  def leaves(self):
    l = frozenset(self._leaves)
    if self._parent:
      l = l | self._parent.leaves()
    return l

  # TODO(adamb) Properly nest names for parents.
  def unique_name(self, root):
    if not root in self._root_suffixes:
      self._root_suffixes[root] = -1

    suffix = self._root_suffixes[root]
    suffix = suffix + 1
    self._root_suffixes[root] = suffix

    return "%s_%s" % (root, suffix)

  def __str__(self):
    return "%s" % self._locals

class TopLevel:
  TYPES = {
	  "half": tf.float16,
	  "float": tf.float32,
	  "double": tf.float64,
	  "int8": tf.int8,
	  "int16": tf.int16,
	  "int32": tf.int32,
	  "int64": tf.int64,
	  "uint8": tf.uint8,
	  "uint16": tf.uint16,
	  "string": tf.string,
	  "bool": tf.bool,
	  "complex64": tf.complex64,
	  "complex128": tf.complex128,
	  "qint8": tf.qint8,
	  "qint32": tf.qint32,
	  "quint": tf.quint8,
  }

  def __init__(self):
    self.nesting_level = 0
    self._global_functions = {}
    self._global_namespaces = {
      "tf": tf,
      "nao": Nao(),
    }

  # "primitive" values
  def _sf_type(self, ctx, name):
    return TopLevel.TYPES[name]

  def _sf_shape(self, ctx, dims):
    return tf.TensorShape(dims)

  def _sf_whole(self, ctx, digits):
    return int(digits)

  def _sf_fraction(self, ctx, decimal):
    return float(decimal)

  def _sf_list(self, ctx, *exprs):
    return [self.__unwrap_bag(self.visit(ctx, expr)) for expr in exprs]

  def __unwrap_bag(self, bag):
    if type(bag) == RetvalBag:
      return bag.get(None)
    return bag

  def _named_tensor(self, ctx, name, shape, dtype, value):
    op = tf.constant(value, shape=shape, dtype=dtype, name=name)
    ctx.possible_leaf(op)

    if name != None:
      ctx.define_local(name, op)

    return op

  def _named_placeholder(self, ctx, name, shape, dtype):
    op = tf.placeholder(dtype, shape=shape, name=name)
    ctx.define_local(name, op)
    return op

  # applying a function
  def _sf_apply(self, ctx, name, ns_name, fn_name, attrs_expr, *arg_exprs):
    attrs = self.visit(ctx, attrs_expr)

    args = []
    for expr in arg_exprs:
      arg = self.visit(ctx, expr)
      if type(arg) == RetvalBag:
        arg = arg.get(None)
      ctx.eliminate_leaf(arg)
      # eprint("arg %s -> %s" % (expr, arg))
      args.append(arg)

    function = None
    if ns_name != None:
      function = ctx.namespace_lookup(ns_name, fn_name)
    else:
      function = ctx.get_local(fn_name)

    if type(function) == RetvalBag:
      function = function.get(None)

    scope_name = name
    if scope_name == None:
      scope_name = ctx.unique_name(fn_name)
    result = function.apply(self, scope_name, attrs, args)

    ctx.possible_leaf(result)
    if name != None:
      ctx.define_local(name, result)

    return result

  def _sf_cond(self, ctx, cond, then, els):
    return tf.cond(
      pred=self.visit(ctx, cond),
      fn1=lambda: self.visit(ctx.subcontext(), then),
      fn2=lambda: self.visit(ctx.subcontext(), els),
    )

  def _sf_local(self, ctx, name):
    # eprint(ctx)
    return ctx.get_local(name)

  def _sf_attr(self, ctx, name):
    # eprint(ctx)
    return ctx.get_attr(name)

  # generating graphs directly
  def visit_graph_exprs(self, ctx, retval_names, exprs):
    for expr in exprs:
      if expr[0] == "__retval":
        name = expr[1]
        subexpr = expr[2]
        op = self.visit(ctx, subexpr)
        ctx.define_local(name, op)
        retval_names.append(name)
      elif expr[0] == "__sf_after_leaves":
        # TODO(adamb) Should actually nest local variables AND leaves
        after_exprs = expr[1:]
        leaves = ctx.leaves()
        with tf.control_dependencies(leaves):
          self.visit_graph_exprs(ctx, retval_names, after_exprs)
      else:
        self.visit(ctx, expr)

  def _sf_graph(self, ctx, name, *exprs):
    with tf.variable_scope(name):
      retval_names = []
      local_ops = ctx.subcontext()

      with tf.variable_scope("_"):
        self.visit_graph_exprs(local_ops, retval_names, exprs)

      for retval_name in retval_names:
        op = local_ops.get_local(retval_name)
        tf.identity(op, name=retval_name)

  def _sf_index(self, ctx, expr, index):
    target = self.visit(ctx, expr)
    return target.get(index)

  def _sf_def_function(self, ctx, name, *rest):
    ctx.set_function(name, rest)

  def _sf_function(self, ctx, name, *rest):
    return DeclaredFunction(ctx, [name, *rest])

  def _sf_attrs(self, ctx, *attr_exprs):
    attrs = {}
    for name, value_expr in attr_exprs:
      attrs[name] = self.visit(ctx, value_expr)
    return attrs

  def visit(self, ctx, expr):
    self.nesting_level = self.nesting_level + 1
    # eprint("%s%s" % ('  ' * self.nesting_level, expr))

    if type(expr) == list:
      expr_type = expr[0]
      attr = getattr(self, expr_type)

      if expr_type.startswith("_sf_"): # Special form
        result = attr(ctx, *expr[1:])
      elif expr_type.startswith("_named_"): # name, then expressions
        result = attr(ctx, expr[1], *[self.visit(ctx, subexpr) for subexpr in expr[2:]])
      else: # just expressions
        result = attr(ctx, *[self.visit(ctx, subexpr) for subexpr in expr[1:]])

      # eprint("visited %s expr %s => %s; ctx: %s" % (expr_type, expr, result, ctx))
      self.nesting_level = self.nesting_level - 1
      return result
    else:
      # eprint("visiting primitive %s ctx: %s" % (expr, ctx))
      self.nesting_level = self.nesting_level - 1
      return expr

def graph_def_from_exprs(exprs):
  with tf.Graph().as_default() as g:
    visitor = TopLevel()
    ctx = Context(visitor._global_namespaces)
    for expr in exprs:
      visitor.visit(ctx, expr)

    return g.as_graph_def()
