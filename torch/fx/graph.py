from .node import Node, Argument, Target, map_arg, _type_repr, _get_qualified_name
import torch.utils._pytree as pytree
from . import _pytree as fx_pytree

from typing import TYPE_CHECKING, Callable, Any, List, Dict, NamedTuple, Optional, Tuple, Set, FrozenSet, Type
from dataclasses import dataclass
from contextlib import contextmanager
import copy
import torch
import keyword
import re
import builtins
import math
import warnings


if TYPE_CHECKING:
    from .graph_module import GraphModule  # noqa: F401
    from ._symbolic_trace import Tracer   # noqa: F401


# Mapping of builtins to their `typing` equivalent.
_origin_type_map = {
    list: List,
    dict: Dict,
    set: Set,
    frozenset: FrozenSet,
    tuple: Tuple,
}

class _CustomBuiltin(NamedTuple):
    """Additional objs that we add to every graph's globals.

    The repr() for some standard library objects is not valid Python code without
    an import. For common objects of this sort, we bundle them in the globals of
    every FX graph.
    """
    # How to import this object from the standard library.
    import_str: str
    # The actual object, produced from that import string.
    obj: Any

_custom_builtins: Dict[str, _CustomBuiltin] = {}


def _register_custom_builtin(name: str, import_str: str, obj: Any):
    _custom_builtins[name] = _CustomBuiltin(import_str, obj)


_register_custom_builtin('inf', 'from math import inf', math.inf)
_register_custom_builtin('nan', 'from math import nan', math.nan)
_register_custom_builtin('NoneType', 'NoneType = type(None)', type(None))
_register_custom_builtin('torch', 'import torch', torch)
_register_custom_builtin('device', 'from torch import device', torch.device)
_register_custom_builtin('fx_pytree', 'import torch.fx._pytree as fx_pytree', fx_pytree)
_register_custom_builtin('pytree', 'import torch.utils._pytree as pytree', pytree)


def _is_magic(x: str) -> bool:
    return x.startswith('__') and x.endswith('__')


def _snake_case(s: str) -> str:
    """
    Transforms the given string ``s`` to a Python-style variable name

    Examples:
        ``mod.snake_case`` -> ``mod.snake_case``
        ``mod.pascalCase``-> ``mod.pascal_case``
        ``mod.ALL_CAPS`` -> ``mod.all_caps``
    """
    chars = []
    prev_lower = False
    for c in s:
        if prev_lower and c.isupper():
            chars.append('_')
        chars.append(c.lower())
        prev_lower = c.islower()
    return ''.join(chars)


def _is_from_torch(obj: Any) -> bool:
    module_name = getattr(obj, '__module__', None)
    if module_name is not None:
        base_module = module_name.partition('.')[0]
        return base_module == 'torch'

    name = getattr(obj, '__name__', None)
    # exclude torch because torch.torch.torch.torch works. idk mang
    if name is not None and name != 'torch':
        for guess in [torch, torch.nn.functional]:
            if getattr(guess, name, None) is obj:
                return True

    return False


class _Namespace:
    """A context for associating names uniquely with objects.

    The following invariants are enforced:
    - Each object gets a single name.
    - Each name is unique within a given namespace.
    - Names generated do not shadow builtins, unless the object is indeed that builtin.
    """
    def __init__(self):
        self._obj_to_name: Dict[Any, str] = {}
        self._unassociated_names = set()
        self._used_names: Dict[str, int] = {}

        self._illegal_char_regex = re.compile('[^0-9a-zA-Z_]+')
        self._name_suffix_regex = re.compile(r"(.*)_(\d+)$")

    def create_name(self, candidate: str, obj: Optional[Any]) -> str:
        """Create a unique name.

        Arguments:
            candidate: used as the basis for the unique name, relevant to the user.
            obj: If not None, an object that will be associated with the unique name.
        """
        if obj is not None and obj in self._obj_to_name:
            return self._obj_to_name[obj]

        # delete all characters that are illegal in a Python identifier
        candidate = self._illegal_char_regex.sub('_', candidate)

        if candidate[0].isdigit():
            candidate = f'_{candidate}'

        match = self._name_suffix_regex.match(candidate)
        if match is None:
            base = candidate
            num = None
        else:
            base, num_str = match.group(1, 2)
            num = int(num_str)

        candidate = base if num is None else f'{base}_{num}'
        num = num if num else 0

        while candidate in self._used_names or self._is_illegal_name(candidate, obj):
            num += 1
            candidate = f'{base}_{num}'

        self._used_names.setdefault(candidate)
        if obj is None:
            self._unassociated_names.add(candidate)
        else:
            self._obj_to_name[obj] = candidate
        return candidate

    def associate_name_with_obj(self, name: str, obj: Any):
        """Associate a unique name with an object.

        Neither `name` nor `obj` should be associated already.
        """
        assert obj not in self._obj_to_name
        assert name in self._unassociated_names
        self._obj_to_name[obj] = name
        self._unassociated_names.remove(name)

    def _is_illegal_name(self, name: str, obj: Any) -> bool:
        # 1. keywords are never allowed as names.
        if name in keyword.kwlist:
            return True

        # 2. Can't shadow a builtin name, unless you *are* that builtin.
        if name in builtins.__dict__:
            return obj is not builtins.__dict__[name]

        # 3. Can't shadow our custom builtins either
        if name in _custom_builtins:
            return obj is not _custom_builtins[name].obj

        return False


@dataclass
class PythonCode:
    """Represents all the information necessary to exec or save a graph as Python code."""
    # Python source code for the forward function definition.
    src: str
    # Values in global scope during exection of `src_def`.
    globals: Dict[str, Any]


def _format_args(args: Tuple[Argument, ...], kwargs: Dict[str, Argument]) -> str:
    args_s = ', '.join(repr(a) for a in args)
    kwargs_s = ', '.join(f'{k} = {repr(v)}' for k, v in kwargs.items())
    if args_s and kwargs_s:
        return f'{args_s}, {kwargs_s}'
    return args_s or kwargs_s

def _format_target(base: str, target: str) -> str:
    elems = target.split('.')
    r = base
    for e in elems:
        if not e.isidentifier():
            r = f'getattr({r}, "{e}")'
        else:
            r = f'{r}.{e}'
    return r

class _InsertPoint:
    def __init__(self, graph, new_insert):
        self.graph = graph
        self.orig_insert, graph._insert = graph._insert, new_insert

    def __enter__(self):
        pass

    def __exit__(self, type, value, tb):
        self.graph._insert = self.orig_insert

class _node_list:
    def __init__(self, graph: 'Graph', direction: str = '_next'):
        assert direction in ['_next', '_prev']
        self.graph = graph
        self.direction = direction

    def __len__(self):
        return self.graph._len

    def __iter__(self):
        root, direction = self.graph._root, self.direction
        cur = getattr(root, direction)
        while cur is not root:
            if not cur._erased:
                yield cur
            cur = getattr(cur, direction)

    def __reversed__(self):
        return _node_list(self.graph, '_next' if self.direction == '_prev' else '_prev')

class _PyTreeInfo(NamedTuple):
    """
    Contains extra info stored when we're using Pytrees
    """
    orig_args: List[str]
    in_spec: pytree.TreeSpec
    out_spec: Optional[pytree.TreeSpec]

class Graph:
    """
    ``Graph`` is the main data structure used in the FX Intermediate Representation.
    It consists of a series of ``Node`` s, each representing callsites (or other
    syntactic constructs). The list of ``Node`` s, taken together, constitute a
    valid Python function.

    For example, the following code

    .. code-block:: python

        import torch
        import torch.fx

        class MyModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 4))
                self.linear = torch.nn.Linear(4, 5)

            def forward(self, x):
                return torch.topk(torch.sum(self.linear(x + self.linear.weight).relu(), dim=-1), 3)

        m = MyModule()
        gm = torch.fx.symbolic_trace(m)

    Will produce the following Graph::

        print(gm.graph)

    .. code-block:: text

        graph(x):
            %linear_weight : [#users=1] = self.linear.weight
            %add_1 : [#users=1] = call_function[target=operator.add](args = (%x, %linear_weight), kwargs = {})
            %linear_1 : [#users=1] = call_module[target=linear](args = (%add_1,), kwargs = {})
            %relu_1 : [#users=1] = call_method[target=relu](args = (%linear_1,), kwargs = {})
            %sum_1 : [#users=1] = call_function[target=torch.sum](args = (%relu_1,), kwargs = {dim: -1})
            %topk_1 : [#users=1] = call_function[target=torch.topk](args = (%sum_1, 3), kwargs = {})
            return topk_1

    For the semantics of operations represented in the ``Graph``, please see :class:`Node`.
    """
    def __init__(self, owning_module: Optional["GraphModule"] = None, tracer_cls: Optional[Type["Tracer"]] = None):
        """
        Construct an empty Graph.
        """
        self._root : Node = Node(self, '', 'root', '', (), {})
        self._used_names : Dict[str, int] = {}  # base name -> number
        self._insert = self._root.prepend
        self._len = 0
        self._graph_namespace = _Namespace()
        self._owners = 0
        self._owning_module = owning_module
        self._tracer_cls = tracer_cls
        self._pytree_info: Optional[_PyTreeInfo] = None

    @property
    def owning_module(self):
        return self._owning_module

    @owning_module.setter
    def owning_module(self, mod: Optional["GraphModule"]):
        if mod:
            self._owning_module = mod if not self._owners else None
            self._owners += 1

    @property
    def nodes(self) -> _node_list:
        """
        Get the list of Nodes that constitute this Graph.

        Note that this ``Node`` list representation is a doubly-linked list. Mutations
        during iteration (e.g. delete a Node, add a Node) are safe.

        Returns:

            A doubly-linked list of Nodes. Note that ``reversed`` can be called on
            this list to switch iteration order.
        """
        return _node_list(self)

    def graph_copy(self, g : 'Graph', val_map : Dict[Node, Node], return_output_node=False) -> 'Optional[Argument]':
        """
        Copy all nodes from a given graph into ``self``.

        Args:

            g (Graph): The source graph from which to copy Nodes.

            val_map (Dict[Node, Node]): a dictionary that will be populated with a mapping
                from nodes in ``g`` to nodes in ``self``. Note that ``val_map`` can be passed
                in with values in it already to override copying of certain values.

        Returns:

            The value in ``self`` that is now equivalent to the output value in ``g``,
            if ``g`` had an ``output`` node. ``None`` otherwise.
        """
        for node in g.nodes:
            if node in val_map:
                continue
            if node.op == 'output':
                rv = map_arg(node.args[0], lambda n: val_map[n])
                return rv if not return_output_node else (rv, node)
            val_map[node] = self.node_copy(node, lambda n : val_map[n])
        return None

    def __deepcopy__(self, memo=None) -> 'Graph':
        """
        Explicitly implement __deepcopy__ to prevent excessive recursion depth
        from the default implementation. This uses graph_copy to copy the nodes
        in an iterative way, rather than recursive. It also populates the
        memoization table to prevent unnecessary copies (e.g. references to
        nodes or other parts of the Graph from a custom GraphModule implementation
        """
        memo = memo if memo else {}
        g = Graph(tracer_cls=self._tracer_cls)
        output_vals = g.graph_copy(self, val_map=memo, return_output_node=True)
        assert isinstance(output_vals, tuple)
        output_val, old_output_val = output_vals
        g.output(output_val, type_expr=getattr(old_output_val, 'type', None))
        return g

    def create_node(self, op: str, target: 'Target',
                    args: Optional[Tuple['Argument', ...]] = None,
                    kwargs: Optional[Dict[str, 'Argument']] = None,
                    name: Optional[str] = None,
                    type_expr: Optional[Any] = None) -> Node:
        """
        Create a ``Node`` and add it to the ``Graph`` at the current insert-point.
        Note that the current insert-point can be set via :meth:`Graph.inserting_before`
        and :meth:`Graph.inserting_after`.

        Args:
            op (str): the opcode for this Node. One of 'call_function', 'call_method', 'get_attr',
                'call_module', 'placeholder', or 'output'. The semantics of these opcodes are
                described in the ``Graph`` docstring.

            args (Optional[Tuple[Argument, ...]]): is a tuple of arguments to this node.

            kwargs (Optional[Dict[str, Argument]]): the kwargs of this Node

            name (Optional[str]): an optional string name for the ``Node``.
                This will influence the name of the value assigned to in the
                Python generated code.

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have.

        Returns:

            The newly-created and inserted node.
        """
        assert op in ('call_function', 'call_method', 'get_attr', 'call_module', 'placeholder', 'output')
        args = () if args is None else args
        kwargs = {} if kwargs is None else kwargs
        assert isinstance(args, tuple), "args must be a tuple"
        assert isinstance(kwargs, dict), "kwargs must be a dict"

        candidate = name if name is not None else self._target_to_str(target)
        name = self._graph_namespace.create_name(candidate, None)
        n = Node(self, name, op, target, args, kwargs, type_expr)

        self._graph_namespace.associate_name_with_obj(name, n)

        self._insert(n)
        self._len += 1
        return n

    def flatten_inps(self, *args):
        flat_args, args_spec = pytree.tree_flatten(args)
        return flat_args

    def unflatten_outs(self, out):
        if self._pytree_info is None:
            return out
        if not isinstance(out, list):
            out = [out]
        assert(self._pytree_info.out_spec is not None)
        return pytree.tree_unflatten(out, self._pytree_info.out_spec)

    def erase_node(self, to_erase : Node) -> None:
        """
        Erases a ``Node`` from the ``Graph``. Throws an exception if
        there are still users of that node in the ``Graph``.

        Args:

            to_erase (Node): The ``Node`` to erase from the ``Graph``.
        """
        if len(to_erase.users) > 0:
            raise RuntimeError(f'Tried to erase Node {to_erase} but it still had {len(to_erase.users)} '
                               f'users in the graph: {to_erase.users}!')

        to_erase._remove_from_list()
        to_erase._erased = True  # iterators may retain handles to erased nodes
        self._len -= 1

        # Null out this Node's argument nodes so that the Nodes referred to
        # can update their ``users`` accordingly
        new_args = map_arg(to_erase.args, lambda n: None)
        assert isinstance(new_args, tuple)
        to_erase.args = new_args
        new_kwargs = map_arg(to_erase.kwargs, lambda n: None)
        assert isinstance(new_kwargs, dict)
        to_erase.kwargs = new_kwargs

    def inserting_before(self, n: Optional[Node] = None):
        """Set the point at which create_node and companion methods will insert into the graph.
        When used within a 'with' statement, this will temporary set the insert point and
        then restore it when the with statement exits::

            with g.inserting_before(n):
                ... # inserting before node n
            ... # insert point restored to what it was previously
            g.inserting_before(n) #  set the insert point permanently

        Args:
            n (Optional[Node]): The node before which to insert. If None this will insert before
              the beginning of the entire graph.

        Returns:
            A resource manager that will restore the insert point on ``__exit__``.
        """
        if n is None:
            return self.inserting_after(self._root)
        assert n.graph == self, "Node to insert before is not in graph."
        return _InsertPoint(self, n.prepend)

    def inserting_after(self, n: Optional[Node] = None):
        """Set the point at which create_node and companion methods will insert into the graph.
        When used within a 'with' statement, this will temporary set the insert point and
        then restore it when the with statement exits::

            with g.inserting_after(n):
                ... # inserting after node n
            ... # insert point restored to what it was previously
            g.inserting_after(n) #  set the insert point permanently

        Args:
            n (Optional[Node]): The node before which to insert. If None this will insert after
              the beginning of the entire graph.

        Returns:
            A resource manager that will restore the insert point on ``__exit__``.
        """
        if n is None:
            return self.inserting_before(self._root)
        assert n.graph == self, "Node to insert after is not in graph."
        return _InsertPoint(self, n.append)

    # sugar for create_node when you know the op
    def placeholder(self, name: str, type_expr: Optional[Any] = None) -> Node:
        """
        Insert a ``placeholder`` node into the Graph. A ``placeholder`` represents
        a function input.

        Args:

            name (str): A name for the input value. This corresponds to the name
                of the positional argument to the function this ``Graph`` represents.

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have. This is needed in some
                cases for proper code generation (e.g. when the function is used
                subsequently in TorchScript compilation).

        .. note::
            The same insertion point and type expression rules apply for this method
            as ``Graph.create_node``.
        """
        return self.create_node('placeholder', name, type_expr=type_expr)

    def get_attr(self, qualified_name: str, type_expr: Optional[Any] = None) -> Node:
        """
        Insert a ``get_attr`` node into the Graph. A ``get_attr`` ``Node`` represents the
        fetch of an attribute from the ``Module`` hierarchy.

        Args:

            qualified_name (str): the fully-qualified name of the attribute to be retrieved.
                For example, if the traced Module has a submodule named ``foo``, which has a
                submodule named ``bar``, which has an attribute named ``baz``, the qualified
                name ``foo.bar.baz`` should be passed as ``qualified_name``.

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have.


        Returns:

            The newly-created and inserted ``get_attr`` node.

        .. note::
            The same insertion point and type expression rules apply for this method
            as ``Graph.create_node``.
        """
        def _get_attr_reference_exists(mod: torch.nn.Module, qualified_name: str) -> bool:
            module_path, _, name = qualified_name.rpartition(".")

            submod: Optional[torch.nn.Module] = mod.get_submodule(module_path)

            if not submod:
                return False

            if not hasattr(submod, name):
                return False

            res = getattr(submod, name)

            if (not isinstance(res, torch.nn.Module)
                    and not isinstance(res, torch.nn.Parameter)
                    and name not in submod._buffers):
                return False

            return True

        if (self.owning_module and
                not _get_attr_reference_exists(self.owning_module, qualified_name)):
            warnings.warn("Attempted to insert a get_attr Node with no "
                          "underlying reference in the owning "
                          "GraphModule! Call "
                          "GraphModule.add_submodule to add the "
                          "necessary submodule, "
                          "GraphModule.add_parameter to add the "
                          "necessary Parameter, or "
                          "nn.Module.register_buffer to add the "
                          "necessary buffer")
        return self.create_node('get_attr', qualified_name, type_expr=type_expr)

    def call_module(self,
                    module_name: str,
                    args: Optional[Tuple['Argument', ...]] = None,
                    kwargs: Optional[Dict[str, 'Argument']] = None,
                    type_expr: Optional[Any] = None) -> Node:
        """
        Insert a ``call_module`` ``Node`` into the ``Graph``. A ``call_module`` node
        represents a call to the forward() function of a ``Module`` in the ``Module``
        hierarchy.

        Args:

            module_name (str): The qualified name of the ``Module`` in the ``Module``
                hierarchy to be called. For example, if the traced ``Module`` has a
                submodule named ``foo``, which has a submodule named ``bar``, the
                qualified name ``foo.bar`` should be passed as ``module_name`` to
                call that module.

            args (Optional[Tuple[Argument, ...]]): The positional arguments to be passed
                to the called method. Note that this should *not* include a ``self`` argument.

            kwargs (Optional[Dict[str, Argument]]): The keyword arguments to be passed
                to the called method

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have.

        Returns:

            The newly-created and inserted ``call_module`` node.

        .. note::
            The same insertion point and type expression rules apply for this method
            as :meth:`Graph.create_node`.
        """
        if (self.owning_module and
                self.owning_module.get_submodule(module_name) is None):
            warnings.warn("Attempted to insert a call_module Node with "
                          "no underlying reference in the owning "
                          "GraphModule! Call "
                          "GraphModule.add_submodule to add the "
                          "necessary submodule")
        return self.create_node('call_module', module_name, args, kwargs, type_expr=type_expr)

    def call_method(self,
                    method_name: str,
                    args: Optional[Tuple['Argument', ...]] = None,
                    kwargs: Optional[Dict[str, 'Argument']] = None,
                    type_expr: Optional[Any] = None) -> Node:
        """
        Insert a ``call_method`` ``Node`` into the ``Graph``. A ``call_method`` node
        represents a call to a given method on the 0th element of ``args``.

        Args:

            method_name (str): The name of the method to apply to the self argument.
                For example, if args[0] is a ``Node`` representing a ``Tensor``,
                then to call ``relu()`` on that ``Tensor``, pass ``relu`` to ``method_name``.

            args (Optional[Tuple[Argument, ...]]): The positional arguments to be passed
                to the called method. Note that this *should* include a ``self`` argument.

            kwargs (Optional[Dict[str, Argument]]): The keyword arguments to be passed
                to the called method

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have.

        Returns:

            The newly created and inserted ``call_method`` node.

        .. note::
            The same insertion point and type expression rules apply for this method
            as :meth:`Graph.create_node`.
        """
        return self.create_node('call_method', method_name, args, kwargs, type_expr=type_expr)

    def call_function(self,
                      the_function: Callable[..., Any],
                      args: Optional[Tuple['Argument', ...]] = None,
                      kwargs: Optional[Dict[str, 'Argument']] = None,
                      type_expr: Optional[Any] = None) -> Node:
        """
        Insert a ``call_function`` ``Node`` into the ``Graph``. A ``call_function`` node
        represents a call to a Python callable, specified by ``the_function``. ``the_function``
        can be

        Args:

            the_function (Callable[..., Any]): The function to be called. Can be any PyTorch
                operator, Python function, or member of the ``builtins`` or ``operator``
                namespaces.

            args (Optional[Tuple[Argument, ...]]): The positional arguments to be passed
                to the called function.

            kwargs (Optional[Dict[str, Argument]]): The keyword arguments to be passed
                to the called function

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have.

        Returns

            The newly created and inserted ``call_function`` node.

        .. note::
            The same insertion point and type expression rules apply for this method
            as :meth:`Graph.create_node`.
        """
        return self.create_node('call_function', the_function, args, kwargs, type_expr=type_expr)

    def node_copy(self, node: Node, arg_transform: Callable[[Node], 'Argument'] = lambda x: x) -> Node:
        """
        Copy a node from one graph into another. ``arg_transform`` needs to transform arguments from
        the graph of node to the graph of self. Example::

            # Copying all the nodes in `g` into `new_graph`
            g : torch.fx.Graph = ...
            new_graph = torch.fx.graph()
            value_remap = {}
            for node in g.nodes:
                value_remap[node] = new_graph.node_copy(node, lambda n : value_remap[n])

        Args:

            node (Node): The node to copy into ``self``.

            arg_transform (Callable[[Node], Argument]): A function that transforms
                ``Node`` arguments in node's ``args`` and ``kwargs`` into the
                equivalent argument in ``self``. In the simplest case, this should
                retrieve a value out of a table mapping Nodes in the original
                graph to ``self``.
        """
        args = map_arg(node.args, arg_transform)
        kwargs = map_arg(node.kwargs, arg_transform)
        assert isinstance(args, tuple)
        assert isinstance(kwargs, dict)
        result_node = self.create_node(node.op, node.target, args, kwargs, node.name, node.type)
        result_node.meta = copy.copy(node.meta)
        return result_node

    def output(self, result: 'Argument', type_expr: Optional[Any] = None):
        """
        Insert an ``output`` ``Node`` into the ``Graph``. An ``output`` node represents
        a ``return`` statement in Python code. ``result`` is the value that should
        be returned.

        Args:

            result (Argument): The value to be returned.

            type_expr (Optional[Any]): an optional type annotation representing the
                Python type the output of this node will have.

        .. note::

            The same insertion point and type expression rules apply for this method
            as ``Graph.create_node``.
        """
        return self.create_node(op='output', target='output', args=(result,), type_expr=type_expr)

    def _target_to_str(self, target : Target) -> str:
        if callable(target):
            op = target.__name__
        else:
            assert isinstance(target, str)
            op = target
            if _is_magic(op):
                op = op[2:-2]
        op = _snake_case(op)
        return op

    def python_code(self, root_module: str) -> PythonCode:
        """
        Turn this ``Graph`` into valid Python code.

        Args:

            root_module (str): The name of the root module on which to look-up
                qualified name targets. This is usually 'self'.

        Returns:

            A PythonCode object, consisting of two fields:
                src: the Python source code representing the object
                globals: a dictionary of global names in `src` -> the objects that they reference.
        """
        # NOTE: [Graph Namespaces]
        #
        # There are two types of symbols in generated Python source code:
        # locals and globals.
        #   Locals are locally defined by the output of a node in the Graph.
        #   Globals are references to external objects, like functions or types.
        #
        # When generating Python code, we need to make sure to name things
        # appropriately. In particular:
        # - All names should be unique, to avoid weird shadowing bugs.
        # - These names need to be consistent, e.g. a object should always be
        #   referenced by the same name.
        #
        # To do this, we create a new namespace just for this source. All names
        # that get printed must come from this namespace.
        #
        # Why can't we re-use node.name? Because it was generated within the
        # namespace `self._graph_namespace`. In order to provide uniqueness
        # over both locals (node.name) *and* globals, we create a completely
        # new namespace to put all identifiers in.
        namespace = _Namespace()

        # Override Node's repr to generate a valid name within our namespace.
        # Since repr() is designed to produce a valid Python expression, it
        # makes sense to re-use it. This way, it's easy to print something like
        # Tuple[Node, Node] by simply calling repr() on it. Node's __repr__ is
        # implemented cooperatively to allow this.
        def node_repr(n: Node):
            return namespace.create_name(n.name, n)

        @contextmanager
        def override_node_repr(graph: Graph):
            orig_repr_fns = {}
            for node in graph.nodes:
                orig_repr_fns[node] = node._repr_fn
                node._repr_fn = node_repr
            try:
                yield None
            finally:
                # restore the original repr functions
                for node in graph.nodes:
                    node._repr_fn = orig_repr_fns[node]

        with override_node_repr(self):
            return self._python_code(root_module, namespace)

    def _python_code(self, root_module: str, namespace: _Namespace) -> PythonCode:
        free_vars: List[str] = []
        body: List[str] = []
        globals_: Dict[str, Any] = {}
        wrapped_fns: Dict[str, None] = {}

        # Wrap string in list to pass by reference
        maybe_return_annotation : List[str] = ['']

        def add_global(name_hint: str, obj: Any):
            """Add an obj to be tracked as a global.

            We call this for names that reference objects external to the
            Graph, like functions or types.

            Returns: the global name that should be used to reference 'obj' in generated source.
            """
            if _is_from_torch(obj) and obj != torch.device:  # to support registering torch.device
                # HACK: workaround for how torch custom ops are registered. We
                # can't import them like normal modules so they must retain their
                # fully qualified name.
                return _get_qualified_name(obj)

            # normalize the name hint to get a proper identifier
            global_name = namespace.create_name(name_hint, obj)

            if global_name in globals_:
                assert globals_[global_name] is obj
                return global_name
            globals_[global_name] = obj
            return global_name

        # Pre-fill the globals table with registered builtins.
        for name, (_, obj) in _custom_builtins.items():
            add_global(name, obj)

        def type_repr(o : Any):
            if o == ():
                # Empty tuple is used for empty tuple type annotation Tuple[()]
                return '()'

            typename = _type_repr(o)

            # This is a generic type, e.g. typing.List[torch.Tensor]
            if hasattr(o, '__origin__'):
                origin_type = _origin_type_map.get(o.__origin__, o.__origin__)
                origin_typename = add_global(_type_repr(origin_type), origin_type)

                # Assign global names for each of the inner type variables.
                args = [type_repr(arg) for arg in o.__args__]

                return f'{origin_typename}[{",".join(args)}]'

            # Common case: this is a regular module name like 'foo.bar.baz'
            return add_global(typename, o)

        # Run through reverse nodes and record the first instance of a use
        # of a given node. This represents the *last* use of the node in the
        # execution order of the program, which we will use to free unused
        # values
        node_to_last_use : Dict[Node, Node] = {}
        user_to_last_uses : Dict[Node, List[Node]] = {}

        def register_last_uses(n : Node, user : Node):
            if n not in node_to_last_use:
                node_to_last_use[n] = user
                user_to_last_uses.setdefault(user, []).append(n)

        for node in reversed(self.nodes):
            map_arg(node.args, lambda n: register_last_uses(n, node))
            map_arg(node.kwargs, lambda n: register_last_uses(n, node))

        def delete_unused_values(user : Node):
            """
            Delete values after their last use. This ensures that values that are
            not used in the remainder of the code are freed and the memory usage
            of the code is optimal.
            """
            if user.op == 'placeholder':
                return
            if user.op == 'output':
                body.append('\n')
                return
            nodes_to_delete = user_to_last_uses.get(user, [])
            if len(nodes_to_delete):
                to_delete_str = ' = '.join([repr(n) for n in nodes_to_delete] + ['None'])
                body.append(f';  {to_delete_str}\n')
            else:
                body.append('\n')


        def emit_node(node : Node):
            maybe_type_annotation = '' if node.type is None else f' : {type_repr(node.type)}'
            if node.op == 'placeholder':
                assert isinstance(node.target, str)
                maybe_default_arg = '' if not node.args else f' = {repr(node.args[0])}'
                free_vars.append(f'{node.target}{maybe_type_annotation}{maybe_default_arg}')
                raw_name = node.target.replace('*', '')
                if raw_name != repr(node):
                    body.append(f'{repr(node)} = {raw_name}\n')
                return
            elif node.op == 'call_method':
                assert isinstance(node.target, str)
                body.append(
                    f'{repr(node)}{maybe_type_annotation} = {_format_target(repr(node.args[0]), node.target)}'
                    f'({_format_args(node.args[1:], node.kwargs)})')
                return
            elif node.op == 'call_function':
                assert callable(node.target)
                # pretty print operators
                if node.target.__module__ == '_operator' and node.target.__name__ in magic_methods:
                    assert isinstance(node.args, tuple)
                    body.append(f'{repr(node)}{maybe_type_annotation} = '
                                f'{magic_methods[node.target.__name__].format(*(repr(a) for a in node.args))}')
                    return
                qualified_name = _get_qualified_name(node.target)
                global_name = add_global(qualified_name, node.target)
                if global_name == 'getattr' and \
                   isinstance(node.args, tuple) and \
                   isinstance(node.args[1], str) and \
                   node.args[1].isidentifier():
                    # pretty print attribute access
                    body.append(f'{repr(node)}{maybe_type_annotation} = {_format_target(repr(node.args[0]), node.args[1])}')
                    return
                body.append(f'{repr(node)}{maybe_type_annotation} = {global_name}({_format_args(node.args, node.kwargs)})')
                if node.meta.get('is_wrapped', False):
                    wrapped_fns.setdefault(global_name)
                return
            elif node.op == 'call_module':
                assert isinstance(node.target, str)
                body.append(f'{repr(node)}{maybe_type_annotation} = '
                            f'{_format_target(root_module, node.target)}({_format_args(node.args, node.kwargs)})')
                return
            elif node.op == 'get_attr':
                assert isinstance(node.target, str)
                body.append(f'{repr(node)}{maybe_type_annotation} = {_format_target(root_module, node.target)}')
                return
            elif node.op == 'output':
                if node.type is not None:
                    maybe_return_annotation[0] = f" -> {type_repr(node.type)}"
                if self._pytree_info is None:
                    body.append(f'return {repr(node.args[0])}')
                else:
                    body.append(f'return pytree.tree_unflatten({repr(node.args[0])}, self._out_spec)')
                return
            raise NotImplementedError(f'node: {node.op} {node.target}')

        for node in self.nodes:
            # NOTE: emit_node does not emit a string with newline. It depends
            # on delete_unused_values to append one
            emit_node(node)
            delete_unused_values(node)

        if len(body) == 0:
            # If the Graph has no non-placeholder nodes, no lines for the body
            # have been emitted. To continue to have valid Python code, emit a
            # single pass statement
            body.append('pass\n')
        if self._pytree_info is not None:
            orig_args = self._pytree_info.orig_args
            has_orig_self = (orig_args[0] == 'self')
            if has_orig_self:
                free_vars.insert(0, 'self')
            if len(free_vars) > 0:  # pytree has placeholders in it
                body.insert(0, f"{', '.join(free_vars)}, = fx_pytree.tree_flatten_spec([{', '.join(orig_args)}], self._in_spec)\n")
        else:
            orig_args = free_vars

        if len(wrapped_fns) > 0:
            wrap_name = add_global('wrap', torch.fx.wrap)
            wrap_stmts = '\n'.join([f'{wrap_name}("{name}")' for name in wrapped_fns])
        else:
            wrap_stmts = ''

        # If the original function didn't have self as its first argument, we
        # would have added it.
        if len(orig_args) == 0 or orig_args[0] != 'self':
            orig_args.insert(0, 'self')
        code = ''.join(body)
        code = '\n'.join('    ' + line for line in code.split('\n'))
        fn_code = f"""
{wrap_stmts}

def forward({', '.join(orig_args)}){maybe_return_annotation[0]}:
{code}"""
        return PythonCode(fn_code, globals_)

    def __str__(self) -> str:
        """
        Print a human-readable (not machine-readable) string representation
        of this Graph
        """
        placeholder_names : List[str] = []
        # This is a one-element array just so ``format_node`` can modify the closed
        # over value
        maybe_return_typename : List[str] = ['']

        node_strs = [node.format_node(placeholder_names) for node in self.nodes]
        param_str = ', '.join(placeholder_names)
        s = f'graph({param_str}){maybe_return_typename[0]}:'
        for node_str in node_strs:
            if node_str:
                s += '\n    ' + node_str
        return s

    def print_tabular(self):
        """
        Prints the intermediate representation of the graph in tabular
        format.
        """
        try:
            from tabulate import tabulate
        except ImportError:
            print("`print_tabular` relies on the library `tabulate`, "
                  "which could not be found on this machine. Run `pip "
                  "install tabulate` to install the library.")
        node_specs = [[n.op, n.name, n.target, n.args, n.kwargs]
                      for n in self.nodes]
        print(tabulate(node_specs,
              headers=['opcode', 'name', 'target', 'args', 'kwargs']))

    def lint(self):
        """
        Runs various checks on this Graph to make sure it is well-formed. In
        particular:
        - Checks Nodes have correct ownership (owned by this graph)
        - Checks Nodes appear in topological order
        - If this Graph has an owning GraphModule, checks that targets
        exist in that GraphModule
        """

        # Check topo order
        def check_arg(arg : Node, n : Optional[Node] = None) -> None:
            context_str = f' of Node \'{n}\' ' if n else ' '
            if arg.graph is not self:
                raise RuntimeError(f'Argument \'{arg}\'{context_str}does not belong to this Graph, '
                                   f'but was used as an argument! If you are copying nodes from another graph, make '
                                   f'sure to use ``arg_transform`` on node_copy() to remap values\n{self}')
            if arg not in seen_values:
                raise RuntimeError(f'Argument \'{arg}\'{context_str}was used before it has been '
                                   f'defined! Please check that Nodes in the graph are topologically ordered\n{self}')

        seen_names : Set[str] = set()
        seen_values : Set[Node] = set()
        for node in self.nodes:
            if node.op not in ['placeholder', 'call_method', 'call_module', 'call_function', 'get_attr', 'output']:
                raise RuntimeError(f'Node {node} had unknown opcode {node.op}!')
            if node.graph is not self:
                raise RuntimeError(f'Node \'{node}\' does not belong to this Graph!')
            map_arg(node.args, lambda arg: check_arg(arg, node))
            map_arg(node.kwargs, lambda arg: check_arg(arg, node))
            seen_values.add(node)

            if node.name in seen_names:
                raise RuntimeError(f'Node redefined name {node.name}!')
            seen_names.add(node.name)

        # Check targets are legit
        if self.owning_module:
            for node in self.nodes:
                if node.op == 'call_function':
                    if not callable(node.target):
                        raise ValueError(f'Node {node} target {node.target} has type {torch.typename(node.target)} but '
                                         'a Callable is expected')
                else:
                    if not isinstance(node.target, str):
                        raise ValueError(f'Node {node} target {node.target} has type {torch.typename(node.target)} but '
                                         'a str is expected')
                if node.op in ['get_attr', 'call_module']:
                    target_atoms = node.target.split('.')
                    m_itr = self.owning_module
                    for i, atom in enumerate(target_atoms):
                        new_m_itr = getattr(m_itr, atom, None)
                        seen_qualname = '.'.join(target_atoms[:i])
                        if new_m_itr is None:
                            raise RuntimeError(f'Node {node} target {node.target} references nonexistent attribute '
                                               f'{atom} of {seen_qualname}')
                        if (node.op == "call_module"
                                and not isinstance(new_m_itr, torch.nn.Module)):
                            raise RuntimeError(f'Node {node} target {node.target} {atom} of {seen_qualname} does '
                                               'not reference an nn.Module')
                        elif (node.op == "get_attr"
                              and not isinstance(new_m_itr, torch.nn.Module)
                              and not isinstance(new_m_itr, torch.nn.Parameter)
                              and atom not in m_itr._buffers):
                            warnings.warn(f'Node {node} target {node.target} {atom} of {seen_qualname} does '
                                          'not reference an nn.Module, nn.Parameter, or buffer, which is '
                                          'what \'get_attr\' Nodes typically target')
                        else:
                            m_itr = new_m_itr

    def eliminate_dead_code(self):
        """
        Remove all dead code from the graph, based on each node's number of
        users, and whether the nodes have any side effects. The graph must be
        topologically sorted before calling.

        Returns:
          bool: Whether the graph was changed as a result of the pass.

        Example:

        Before dead code is eliminated, `a` from `a = x + 1` below has no users
        and thus can be eliminated from the graph without having an effect.

        .. code-block:: python

            def forward(self, x):
                a = x + 1
                return x + self.attr_1

        After dead code is eliminated, `a = x + 1` has been removed, and the rest
        of `forward` remains.

        .. code-block:: python

            def forward(self, x):
                return x + self.attr_1

        """
        # Lint the graph first to make sure its topologically sorted, otherwise
        # DCE below will not behave as expected.
        self.lint()

        # Reverse iterate so that when we remove a node, any nodes used as an
        # input to that node have an updated user count that no longer reflects
        # the removed node.
        changed = False
        for node in reversed(self.nodes):
            if not node.is_impure() and len(node.users) == 0:
                self.erase_node(node)
                changed = True

        return changed


reflectable_magic_methods = {
    'add': '{} + {}',
    'sub': '{} - {}',
    'mul': '{} * {}',
    'floordiv': '{} // {}',
    'truediv': '{} / {}',
    'div': '{} / {}',
    'mod': '{} % {}',
    'pow': '{} ** {}',
    'lshift': '{} << {}',
    'rshift': '{} >> {}',
    'and': '{} & {}',
    'or': '{} | {}',
    'xor': '{} ^ {}',
    'getitem': '{}[{}]'
}

magic_methods = dict({
    'eq': '{} == {}',
    'ne': '{} != {}',
    'lt': '{} < {}',
    'gt': '{} > {}',
    'le': '{} <= {}',
    'ge': '{} >= {}',
    'pos': '+{}',
    'neg': '-{}',
    'invert': '~{}'}, **reflectable_magic_methods)
