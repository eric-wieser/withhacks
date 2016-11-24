"""

  withhacks:  building blocks for with-statement-related hackery

This module is a collection of useful building-blocks for hacking the Python
"with" statement.  It combines ideas from several neat with-statement hacks 
I found around the internet into a suite of re-usable components:

  * http://www.mechanicalcat.net/richard/log/Python/Something_I_m_working_on.3
  * http://billmill.org/multi_line_lambdas.html
  * http://code.google.com/p/ouspg/wiki/AnonymousBlocksInPython

By subclassing the appropriate context managers from this module, you can
easily do things such as:

  * skip execution of the code inside the with-statement
  * set local variables in the frame executing the with-statement
  * capture the bytecode from inside the with-statement
  * capture local variables defined inside the with-statement

Building on these basic tools, this module also provides some useful prebuilt
hacks:

  :xargs:      call a function with additional arguments defined in the
               body of the with-statement
  :xkwargs:    call a function with additional keyword arguments defined
               in the body of the with-statement
  :namespace:  direct all variable accesses and assignments to the attributes
               of a given object (like "with" in JavaScript or VB)
  :keyspace:   direct all variable accesses and assignments to the keys of
               of a given object (like namespace() but for dicts)

WithHacks makes extensive use of Noam Raphael's fantastic "byteplay" module;
since the official byteplay distribution doesn't support Python 2.6, a local
version with appropriate patches is included in this module.

"""
from __future__ import with_statement
from .__about__ import *

import sys
import types
import copy
import inspect
import bytecode

try:
    import threading
except ImportError:
    import dummy_threading as threading

from withhacks.frameutils import load_name, extract_code, inject_trace_func


class _ExitContext(Exception):
    """Special exception used to skip execution of a with-statement block."""
    pass


def _exit_context(frame):
    """Simple function to throw an _ExitContext exception."""
    raise _ExitContext


class _Bucket:
    """Anonymous attribute-bucket class."""
    pass



class WithHack(object):
    """Base class for with-statement-related hackery.

    This class provides some useful utilities for constructing with-statement
    hacks.  Specifically:

        * ability to skip execution of the contained block of code
        * ability to access the frame of execution containing the block
        * ability to update local variables in the execution frame

    If a subclass sets the attribute "dont_execute" to true then execution
    of the with-statement's contained code block will be skipped.  If it sets
    the attribute "must_execute" to true, the block will be executed regardless
    of the setting of "dont_execute".  Having two settings allows hacks that
    want to skip the block to be combined with hacks that need it executed.
    """

    dont_execute = False
    must_execute = False

    def _get_context_frame(self):
        """Get the frame object corresponding to the with-statement context.

        This is designed to work from within superclass method call. It finds
        the first frame in which the variable "self" is not bound to this 
        object.  While this heuristic rules out some strange uses of WithHack
        objects (such as entering on object inside its own __exit__ method)
        it should suffice in practise.
        """
        try:
            return self.__frame
        except AttributeError:
            # Offset 2 accounts for this method, and the one calling it.
            f = sys._getframe(2)
            while f.f_locals.get("self") is self:
                f = f.f_back
            self.__frame = f
            return f

    def _set_context_locals(self,locals):
        """Set local variables in the with-statement context.

        The argument "locals" is a dictionary of name bindings to be inserted
        into the execution context of the with-statement.
        """
        frame = self._get_context_frame()
        inject_trace_func(frame,lambda frame: frame.f_locals.update(locals))

    def __enter__(self):
        """Enter the context of this WithHack.

        The base implementation will skip execution of the contained
        code according to the values of "dont_execute" and "must_execute".
        Be sure to call the superclass version if you override it.
        """
        if self.dont_execute and not self.must_execute:
            frame = self._get_context_frame()
            inject_trace_func(frame,_exit_context)
        return self

    def __exit__(self,exc_type,exc_value,traceback):
        """Enter the context of this WithHack.

        This is usually where all the interesting hackery takes place.

        The base implementation suppresses the special _ExitContext exception
        but lets any other exceptions pass through.  Your subclass should
        probably do the same - the simplest way is to pass through the return
        value given by this base implementation.
        """
        if exc_type is _ExitContext:
            return True
        else:
            return False


class CaptureBytecode(WithHack):
    """WithHack to capture the bytecode in the scope of a with-statement.

    The captured bytecode is stored as a byteplay.Code object in the attribute
    "bytecode".  Note that there's no guarantee that this sequence of bytecode
    can be turned into a valid code object!  For example, it may not properly
    return a value.

    If the with-statement contains an "as" clause, the name of the variable
    is stored in the attribute "as_name".
    """

    dont_execute = True

    def __init__(self):
        self.__bc_start = None
        self.bytecode = None
        self._as_clause = None
        super(CaptureBytecode,self).__init__()

    def __enter__(self):
        self.__bc_start = self._get_context_frame().f_lasti
        return super(CaptureBytecode,self).__enter__()

    def __exit__(self,*args):
        frame = self._get_context_frame()
        bc = extract_code(frame,self.__bc_start,frame.f_lasti)

        # Remove code setting up the with-statement block.
        while not isinstance(bc[0], bytecode.instr.BaseInstr) or bc[0].name != 'SETUP_WITH':
            bc[:] = bc[1:]
        bc[:] = bc[1:]

        # extract code that belongs to the as clause
        as_clause = copy.copy(bc)
        as_clause[:] = []
        for i, instr in enumerate(bc):
            as_clause.append(instr)
            if instr.name.startswith('STORE') or instr.name == 'POP_TOP':
                break
        bc[:] = bc[i+1:]
        self._as_clause = as_clause

        # remove code tearing down the with-statement block
        while not isinstance(bc[-1], bytecode.instr.BaseInstr) or bc[-1].name != 'POP_BLOCK':
            bc[:] = bc[:-1]
        bc[:] = bc[:-1]

        # save the trimmed bytecode
        self.bytecode = bc
        return super(CaptureBytecode,self).__exit__(*args)

    def _run_as_clause(self, value):
        """
        Run the as clause, setting the target expression to `value`

        This handles arbitrary as clause expressions, like

            with somehack as d['item'][i].foo().bar:
                pass
        """
        assert self._as_clause

        if len(self._as_clause) == 1:
            first = self._as_clause[0]
            # store_fast has to be handled specially
            if first.name == 'STORE_FAST':
                self._set_context_locals({first.arg: value})
                return
            # pop_top is a no-op
            elif first.name == 'POP_TOP':
                return

        # if somehow there's a STORE_FAST in there, it's not going to work
        if any(instr.name == 'STORE_FAST' for instr in self._as_clause):
            raise NotImplementedError("Cannot handle this as clause")

        frame = self._get_context_frame()

        # prepend a LOAD_CONST with a dummy value
        dummy = object()
        code = copy.copy(self._as_clause)
        code[:0] = [bytecode.Instr('LOAD_CONST', dummy)]
        code.extend([
            bytecode.Instr('LOAD_CONST', None),
            bytecode.Instr('RETURN_VALUE')
        ])

        # configure the object
        code.argcount = 0
        code.name = '<as clause>'
        code.flags &= ~inspect.CO_NEWLOCALS

        # fiddle with variable lookups
        self._change_lookups(code, locals=frame.f_locals)

        # now swap out the constant (which would be rejected by to_concrete_bytecode)
        concrete_code = code.to_concrete_bytecode()
        concrete_code.consts[concrete_code.consts.index(dummy)] = value

        # run the assignment in the context frame
        raw_code = concrete_code.to_code()
        exec(raw_code, frame.f_globals, frame.f_locals)


    def _change_lookups(self, code, *, args=(), locals=()):
        """
        Switch name access opcodes as appropriate.
        Any new locals are local to the function; existing locals
        are manipulated using LOAD/STORE/DELETE_NAME.

        TODO: does this work for STORE_FAST, DELETE_FAST?
        """
        for instr in code:
            if not isinstance(instr, bytecode.instr.BaseInstr):
                continue
            if instr.name in ('LOAD_FAST','LOAD_DEREF','LOAD_NAME','LOAD_GLOBAL'):
                if instr.arg in args:
                    instr.name = 'LOAD_FAST'
                elif instr.name in ('LOAD_FAST','LOAD_DEREF',):
                    if instr.arg in locals:
                        instr.name = 'LOAD_NAME'
                    else:
                        instr.name = 'LOAD_FAST'
            elif instr.name in ('STORE_FAST','STORE_DEREF','STORE_NAME','STORE_GLOBAL'):
                if instr.arg in args:
                    instr.name = 'STORE_FAST'
                elif instr.name in ('STORE_FAST','STORE_DEREF',):
                    if instr.arg in locals:
                        instr.name = 'STORE_NAME'
                    else:
                        instr.name = 'STORE_FAST'
            elif instr.name in ('DELETE_FAST','DELETE_NAME','DELETE_GLOBAL'):
                if instr.arg in args:
                    instr.name = 'DELETE_FAST'
                elif instr.name in ('DELETE_FAST',):
                    if instr.arg in locals:
                        instr.name = 'DELETE_NAME'
                    else:
                        instr.name = 'DELETE_FAST'


class CaptureFunction(CaptureBytecode):
    """WithHack to capture contents of with-statement as anonymous function.

    The bytecode of the contained block is converted into a function and
    made available as the attribute "function".  The following arguments
    control the signature of the function:

        * args:       tuple of argument names
        * varargs:    boolean indicating present of a *args argument
        * varkwargs:  boolean indicating present of a *kwargs argument
        * name:       name associated with the function object
        * argdefs:    tuple of default values for arguments

    Here's a quick example:

        >>> with CaptureFunction(("message","times",)) as f:
        ...     for i in range(times):
        ...         print(message)
        ...
        >>> f.function("hello world",2)
        hello world
        hello world
        >>>

    """

    def __init__(self,args=[],varargs=False,varkwargs=False,name="<withhack>",
                      argdefs=()):
        self.__args = args
        self.__varargs = varargs
        self.__varkwargs = varkwargs
        self.__name = name
        self.__argdefs = argdefs
        super(CaptureFunction,self).__init__()

    def __exit__(self,*args):
        frame = self._get_context_frame()
        retcode = super(CaptureFunction,self).__exit__(*args)
        funcode = copy.copy(self.bytecode)
        #  Ensure it's a properly formed func by always returning something
        funcode.append(bytecode.Instr('LOAD_CONST', None))
        funcode.append(bytecode.Instr('RETURN_VALUE'))
        self._change_lookups(funcode, args=self.__args, locals=frame.f_locals)

        #  Create the resulting function object
        # funcode.args = self.__args
        # funcode.varargs = self.__varargs
        # funcode.varkwargs = self.__varkwargs
        funcode.name = self.__name
        funcode.argnames = self.__args
        funcode.argcount = len(self.__args)
        if self.__varargs:
            funcode.flags |= inspect.CO_VARARGS
            funcode.argcount -= 1
        if self.__varkwargs:
            funcode.flags |= inspect.CO_VARKEYWORDS
            funcode.argcount -= 1

        gs = self._get_context_frame().f_globals
        nm = self.__name
        defs = self.__argdefs
        self.function = types.FunctionType(funcode.to_code(),gs,nm,defs)
        return retcode


class CaptureLocals(CaptureBytecode):
    """WithHack to capture any local variables assigned to in the block.

    When the block exits, the attribute "locals" will be a dictionary 
    containing any local variables that were assigned to during the execution
    of the block.

        >>> with CaptureLocals() as f:
        ...     x = 7
        ...     y = 8
        ...
        >>> f.locals == {'y': 8, 'x': 7}
        True
        >>>

    """

    must_execute = True
    dest_type = dict

    def __exit__(self,*args):
        retcode = super(CaptureLocals,self).__exit__(*args)
        frame = self._get_context_frame()
        self.locals = self.dest_type()
        for instr in self.bytecode:
           if instr.name in ('STORE_FAST','STORE_NAME'):
               self.locals[instr.arg] = frame.f_locals[instr.arg]
        return retcode


class CaptureOrderedLocals(CaptureLocals):
    """WithHack to capture local variables modified in the block, in order.

    When the block exits, the attribute "locals" will be a list containing
    a (name,value) pair for each local variable created or modified during
    the execution of the block.   The variables are listed in the order
    they are first assigned.

        >>> with CaptureOrderedLocals() as f:
        ...     x = 7
        ...     y = 8
        ...
        >>> f.locals
        OrderedDict([('x', 7), ('y', 8)])
        >>>

    """
    from collections import OrderedDict as dest_type


class CaptureModifiedLocals(WithHack):
    """WithHack to capture any local variables modified in the block.

    When the block exits, the attribute "locals" will be a dictionary 
    containing any local variables that were created or modified during the
    execution of the block.

        >>> x = 7
        >>> with CaptureModifiedLocals() as f:
        ...     x = 7
        ...     y = 8
        ...     z = 9
        ...
        >>> f.locals == {'y': 8, 'z': 9}
        True
        >>>

    This differs from CaptureLocals in that it does not detect variables
    that are assigned within the block if their value doesn't actually
    change.  It's cheaper to test for but not as reliable.
    """

    def __enter__(self):
        frame = self._get_context_frame()
        self.__pre_locals = frame.f_locals.copy()
        return super(CaptureModifiedLocals,self).__enter__()

    def __exit__(self,*args):
        frame = self._get_context_frame()
        self.locals = {}
        for (name,value) in frame.f_locals.items():
            if value is self:
                pass
            elif name not in self.__pre_locals:
                self.locals[name] = value
            elif self.__pre_locals[name] != value:
                self.locals[name] = value
        del self.__pre_locals
        return super(CaptureModifiedLocals,self).__exit__(*args)


class xargs(CaptureOrderedLocals):
    """WithHack to call a function with arguments defined in the block.

    This WithHack captures the value of any local variables created or 
    modified in the scope of the block, then passes those values as extra
    positional arguments to the given function call.  The result of the
    function call is stored in the "as" variable if given.

        >>> with xargs(filter) as evens:
        ...     def filter_func(i):
        ...         return (i % 2) == 0
        ...     items = range(10)
        ...
        >>> print(list(evens))
        [0, 2, 4, 6, 8]
        >>>

    """

    def __init__(self,func,*args,**kwds):
        self.__func = func
        self.__args = args
        self.__kwds = kwds
        super(xargs,self).__init__()

    def __exit__(self,*args):
        retcode = super(xargs,self).__exit__(*args)
        args_ = [arg for arg in self.__args]
        args_.extend([arg for (nm,arg) in self.locals.items()])
        retval = self.__func(*args_,**self.__kwds)
        self._run_as_clause(retval)
        return retcode


class xkwargs(CaptureLocals,CaptureBytecode):
    """WithHack calling a function with extra keyword arguments.

    This WithHack captures any local variables created during execution of
    the block, then calls the given function using them as extra keyword
    arguments.

        >>> def calculate(a,b):
        ...     return a * b
        ...
        >>> with xkwargs(calculate,b=2) as result:
        ...     a = 5
        ...
        >>> print(result)
        10

    """

    def __init__(self,func,*args,**kwds):
        self.__func = func
        self.__args = args
        self.__kwds = kwds
        super(xkwargs,self).__init__()

    def __exit__(self,*args):
        retcode = super(xkwargs,self).__exit__(*args)
        kwds = self.__kwds.copy()
        kwds.update(self.locals)
        retval = self.__func(*self.__args,**kwds)
        self._run_as_clause(retval)
        return retcode


class namespace(CaptureBytecode):
    """WithHack sending assignments to a specified namespace.

    This WithHack permits a construct simlar to the "with" statement from
    Visual Basic or JavaScript.  Inside a namespace context, all local
    variable accesses are actually accesses to the attributes of that
    object.

        >>> import sys
        >>> with namespace(sys):
        ...     testing = "hello"
        ...     copyright2 = copyright
        ...
        >>> sys.testing
        'hello'
        >>> sys.copyright2 == sys.copyright
        True

    If no object is passed to the constructor, an empty object is created and
    used.  To get a reference to the namespace, use an "as" clause:

        >>> with namespace() as ns:
        ...     x = 1
        ...     y = x + 4
        ...
        >>> print(ns.x); print(ns.y)
        1
        5

    """

    def __init__(self,ns=None):
        if ns is None:
            self.namespace = _Bucket()
        else:
            self.namespace = ns
        super(namespace,self).__init__()

    def __exit__(self,*args):
        frame = self._get_context_frame()
        retcode = super(namespace,self).__exit__(*args)
        # funcode = copy.deepcopy(self.bytecode)
        funcode = copy.copy(self.bytecode)
        #  Ensure it's a properly formed func by always returning something
        funcode.append(bytecode.Instr('LOAD_CONST', None))
        funcode.append(bytecode.Instr('RETURN_VALUE'))
        #  Switch LOAD/STORE/DELETE_FAST/NAME to LOAD/STORE/DELETE_ATTR
        to_replace = []
        for i, instr in enumerate(funcode):
            repl = self._replace_opcode(instr, frame)
            if repl:
                to_replace.append((i, repl))
        offset = 0
        for i, repl in to_replace:
            funcode[i+offset:i+offset+1] = repl
            offset += len(repl) - 1
        #  Create function object to do the manipulation
        funcode.argnames = ("_[namespace]",)
        funcode.argcount = 1
        funcode.name = "<withhack>"
        gs = self._get_context_frame().f_globals
        func = types.FunctionType(funcode.to_code(),gs)
        #  Execute bytecode in context of namespace
        retval = func(self.namespace)

        self._run_as_clause(self.namespace)

        return retcode

    def _replace_opcode(self, instr, frame, *,
                        _load=lambda i: [bytecode.Instr('LOAD_ATTR', i.arg)],
                        _store=lambda i: [bytecode.Instr('STORE_ATTR', i.arg)],
                        _delete=lambda i: [bytecode.Instr('DELETE_ATTR', i.arg)],
                        _exc=AttributeError):
        Instr = bytecode.Instr
        Label = bytecode.Label

        if instr.name in ('STORE_FAST','STORE_NAME',):
            return [Instr('LOAD_FAST',"_[namespace]")] + _store(instr)
        if instr.name in ('DELETE_FAST','DELETE_NAME',):
            return [Instr('LOAD_FAST',"_[namespace]")] + _delete(instr)
        if instr.name in ('LOAD_FAST','LOAD_NAME','LOAD_GLOBAL','LOAD_DEREF'):
            excIn = Label(); excOut = Label(); end = Label()
            # try:
            #     x = namespace.<attr>
            # except AttributeError:
            #     x = load_name(frame, '<attr>')
            return [Instr('SETUP_EXCEPT',excIn),
                        Instr('LOAD_FAST',"_[namespace]")] + _load(instr) + [
                        Instr('STORE_FAST',"_[ns_value]"),
                        Instr('POP_BLOCK'), Instr('JUMP_FORWARD',end),
                    excIn,
                        Instr('DUP_TOP'), Instr('LOAD_CONST',_exc),
                        Instr('COMPARE_OP',bytecode.Compare.EXC_MATCH),
                        Instr('POP_JUMP_IF_FALSE',excOut), Instr('POP_TOP'),
                        Instr('POP_TOP'), Instr('POP_TOP'),
                        Instr('LOAD_CONST',load_name), Instr('LOAD_CONST',frame),
                        Instr('LOAD_CONST',instr.arg), Instr('CALL_FUNCTION',2),
                        Instr('STORE_FAST',"_[ns_value]"),
                        Instr('POP_EXCEPT'),
                        Instr('JUMP_FORWARD',end),
                    excOut,
                        Instr('END_FINALLY'),
                    end,
                        Instr('LOAD_FAST',"_[ns_value]")]
        return None


class keyspace(namespace):
    """WithHack sending assignments to a specified dict-like object.

    This WithHack permits a construct simlar to the "with" statement from
    Visual Basic or JavaScript.  Inside a namespace context, all local
    variable accesses are actually accesses to the keys of that object.

        >>> import sys
        >>> with keyspace(sys.__dict__):
        ...     testing = "hello"
        ...     copyright2 = copyright
        ...
        >>> sys.testing
        'hello'
        >>> sys.copyright2 == sys.copyright
        True

    If no object is passed to the constructor, an empty dict is created and
    used.  To get a reference to the keyspace, use an "as" clause:

        >>> with keyspace() as ks:
        ...     x = 1
        ...     y = x + 4
        ...
        >>> print(ks["x"]); print(ks["y"])
        1
        5

    """

    def __init__(self,ns=None):
        if ns is None:
            ns = {}
        super(keyspace,self).__init__(ns)

    def _replace_opcode(self, instr, frame):
        Instr = bytecode.Instr
        return super()._replace_opcode(instr, frame,
            _load=lambda i: [Instr('LOAD_CONST', i.arg), Instr('BINARY_SUBSCR')],
            _store=lambda i: [Instr('LOAD_CONST', i.arg), Instr('STORE_SUBSCR')],
            _delete=lambda i: [Instr('LOAD_CONST', i.arg), Instr('DELETE_SUBSCR')],
            _exc=KeyError
        )

