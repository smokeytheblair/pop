# -*- coding: utf-8 -*-

# Import python libs
import os
import imp
import inspect
import logging

# Import pop libs
import pop.dirs
import pop.scanner
import pop.loader
import pop.exc
import pop.contract

EXT_SUFFIXES = tuple([suffix[0] for suffix in imp.get_suffixes() if suffix[-1] == imp.C_EXTENSION])
log = logging.getLogger(__name__)


class Hub:
    '''
    The redistributed pop central hub. All components of the system are
    rooted to the Hub.
    '''
    def __init__(self):
        self._subs = {}
        self._add_subsystem(
                'tools',
                pypath='pop.mods.tools')

    def __getstate__(self):
        return dict(
            _subs=self._subs,
        )

    def __setstate__(self, state):
        self.__dict__.update(state)

    def _add_subsystem(
            self,
            modname,
            subname=None,
            pypath=None,
            static=None,
            contracts_pypath=None,
            contracts_static=None,
            default_contracts=None,
            virtual=True,
            recurse=False,
            omit_start=('_'),
            omit_end=(),
            omit_func=False,
            omit_class=True,
            omit_vars=False,
            mod_basename='pop.poped',
            stop_on_failures=False,
            init=None,
            ):
        subname = subname if subname else modname
        self._subs[modname] = Sub(
                self,
                modname,
                subname,
                pypath,
                static,
                contracts_pypath,
                contracts_static,
                default_contracts,
                virtual,
                recurse,
                omit_start,
                omit_end,
                omit_func,
                omit_class,
                omit_vars,
                mod_basename,
                stop_on_failures)
        self._subs[modname]._pop_init(init)

    @property
    def _(self):
        '''
        This function allows for hub to pop introspective calls.
        This should only ever be called from within a hub module, otherwise
        it should stack trace, or return heaven knows what...
        '''
        dirname = os.path.dirname(inspect.stack()[1].filename)
        for sub in self._subs:
            if dirname in self._subs[sub]._dirs:
                return self._subs[sub]
        raise pop.exc.PopLookupError('Called from outside a pop!')

    def _remove_subsystem(self, subname):
        '''
        Remove the named subsystem
        '''
        if subname in self._subs:
            self._subs.pop(subname)
            return True
        return False

    def __getattr__(self, item):
        if item.startswith('_'):
            return self.__getattribute__(item)
        if item in self._subs:
            return self._subs[item]
        return self.__getattribute__(item)


class Sub:
    '''
    The pop object contains the loaded module data
    '''
    def __init__(
            self,
            hub,
            modname,
            subname=None,
            pypath=None,
            static=None,
            contracts_pypath=None,
            contracts_static=None,
            default_contracts=None,
            virtual=True,
            recurse=False,
            omit_start=('_'),
            omit_end=(),
            omit_func=False,
            omit_class=True,
            omit_vars=False,
            mod_basename='pop.poped',
            stop_on_failures=False,
            init=None,
            ):
        self._hub = hub
        self._subs = {}
        self._mem = {}
        self._modname = modname
        self._subname = subname if subname else modname
        self._pypath = pypath
        self._static = static
        self._contracts_pypath = contracts_pypath
        self._contracts_static = contracts_static
        if isinstance(default_contracts, str):
            default_contracts = [default_contracts]
        self._default_contracts = default_contracts or ()
        self._virtual = virtual
        self._recurse = recurse
        self._omit_start = omit_start
        self._omit_end = omit_end
        self._omit_func = omit_func
        self._omit_class = omit_class
        self._omit_vars = omit_vars
        self._mod_basename = mod_basename
        self._stop_on_failures = stop_on_failures
        self._init = init
        self.__prepare__()

    def __prepare__(self):
        self._dirs = pop.dirs.dir_list(self._pypath, self._static)
        self._contract_dirs = pop.dirs.dir_list(self._contracts_pypath, self._contracts_static)
        if self._contract_dirs:
            self._contracts = ContractSub(
                    self._hub,
                    '{}.contracts'.format(self._modname),
                    static=self._contract_dirs)
        else:
            self._contracts = None
        self._mem = {}
        self._scan = pop.scanner.scan(self._dirs, self._recurse)
        self._loaded = {}
        self._vmap = {}
        self._load_errors = {}
        self._loaded_all = False
        # Always do the pop_init last!
        self._pop_init(self._init)

    def __getstate__(self):
        return dict(
            _hub=self._hub,
            _modname=self._modname,
            _subname=self._subname,
            _pypath=self._pypath,
            _static=self._static,
            _contracts_pypath=self._contracts_pypath,
            _contracts_static=self._contracts_static,
            _default_contracts=self._default_contracts,
            _virtual=self._virtual,
            _recurse=self._recurse,
            _omit_start=self._omit_start,
            _omit_end=self._omit_end,
            _omit_func=self._omit_func,
            _omit_class=self._omit_class,
            _omit_vars=self._omit_vars,
            _mod_basename=self._mod_basename,
            _stop_on_failures=self._stop_on_failures,
            _init=self._init
        )

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__prepare__()

    @property
    def _(self):
        '''
        Return the local relative module on this pop, this will not work if
        called from outside a pop.
        '''
        fn = inspect.stack()[1].filename
        vname = self._vmap[fn]
        return getattr(self, vname)

    def __getattr__(self, item):
        '''
        If the item should be loaded, load it, else serve it
        '''
        if item.startswith('_'):
            return self.__getattribute__(item)
        if item in self._loaded:
            ret = self._loaded[item]
            # If this previously errored on load, try it again,
            # it might be ready to load now
            if isinstance(ret, pop.loader.LoadError):
                ret = self._find_mod(item)
                if isinstance(ret, pop.loader.LoadError):
                    # If this is still a LoadError, process it
                    self._process_load_error(ret)
            return ret
        elif item in self._subs:
            return self._subs[item]
        return self._find_mod(item)

    def __contains__(self, item):
        try:
            return hasattr(self, item)
        except pop.exc.PopLookupError:
            return False

    @property
    def __name__(self):
        return '{}.{}'.format(self._mod_basename, self._modname)

    def __iter__(self):
        if self._loaded_all is False:
            self._load_all()
        return iter(self._loaded.values())

    def _pop_init(self, init):
        '''
        Run the new module initializer, basically the __init__ for the pop
        '''
        if not init:
            # No init!
            return
        comps = init.split('.')
        mod = self
        for comp in comps:
            mod = getattr(mod, comp)
        mod()

    def _process_load_error(self, mod, skip_full_stop=False):
        if not isinstance(mod, pop.loader.LoadError):
            # This is not a LoadError, return now!
            return False

        if mod.edict['verror']:
            error = '{0[msg]}: {0[verror]}'.format(mod())
            if skip_full_stop is False and self._stop_on_failures is True:
                raise pop.exc.PopError(error)
            log.info(error)
            return
        error = '{0[msg]}: {0[exception]!r}'.format(mod())
        if mod.traceback:
            error += '\n' + mod.traceback
        if skip_full_stop is False and self._stop_on_failures is True:
            raise pop.exc.PopError(error)
        if mod.traceback:
            log.warning(error)
        else:
            log.info(error)
        return True

    def _find_mod(self, item):
        '''
        find the module named item
        '''
        for iface in self._scan:
            for bname in self._scan[iface]:
                if self._scan[iface][bname].get('loaded'):
                    continue
                self._load_item(iface, bname)
                if item in self._loaded:
                    return self._loaded[item]
        # Let's see if the module being lookup is in the load errors dictionary
        if item in self._load_errors:
            # Return the LoadError
            return self._load_errors[item]

    def _load_item(self, iface, bname):
        '''
        Load the named basename
        '''
        if iface not in self._scan:
            raise pop.exc.PopLoadError('Bad call to load item, no iface {}'.format(iface))
        if bname not in self._scan[iface]:
            raise pop.exc.PopLoadError(
                'Bad call to load item, no bname {} in iface {}'.format(bname, iface))
        mname = '{}.{}'.format(self.__name__, os.path.basename(bname))
        mod = pop.loader.load_mod(
                mname,
                iface,
                self._scan[iface][bname]['path'],
                self
        )
        if self._process_load_error(mod):
            self._load_errors[os.path.basename(bname)] = mod
            return
        self._prep_mod(mod, iface, bname)

    def _prep_mod(self, mod, iface, bname):
        '''
        Prepare the module!
        '''
        vret = pop.loader.load_virtual(
                self._hub,
                self._virtual,
                mod,
                bname)
        if 'error' in vret:
            # Virtual Errors should not full stop pop
            self._process_load_error(vret['error'], skip_full_stop=True)
            # Store the LoadError under the __virtualname__ if defined
            self._load_errors[vret['vname']] = vret['error']
            return

        contracts = pop.contract.load_contract(
                self._contracts,
                self._default_contracts,
                mod)
        name = vret['name']
        if name.endswith(EXT_SUFFIXES):
            for ext in EXT_SUFFIXES:
                if name.endswith(ext):
                    name = name.split(ext)[0]
                    break
        pop.loader.mod_init(self._hub, mod)
        mod_dict = pop.loader.prep_mod_dict(
                self,
                mod,
                name,
                contracts,
                loading_contract_sub=isinstance(self, ContractSub))
        pop.contract.verify_contract(self._hub, contracts, mod_dict)
        self._loaded[name] = mod_dict
        self._vmap[mod.__file__] = name
        # Let's mark the module as loaded
        self._scan[iface][bname]['loaded'] = True

    def _load_all(self):
        '''
        Load all modules found during the scan.

        .. attention:: This completely disables the lazy loader behavior or pop
        '''
        if self._loaded_all is True:
            return
        for iface in self._scan:
            for bname in self._scan[iface]:
                if self._scan[iface][bname].get('loaded'):
                    continue
                self._load_item(iface, bname)
        self._loaded_all = True


class ContractSub(Sub):
    '''
    This class exists to deferentiate regular Pop's from Pop's loading contracts
    '''