""" This module defines the "overlay" that will appear as the `__PIXIE__`
dictionary if the PIXIE compiler is asked to compile the source to a Python
"C-Extension".
"""
import ctypes
import inspect


# Auxilliary functions
def address_of_symbol(DSO, symbol_name):
    return getattr(DSO, symbol_name)  # Fix this


def get_bitcode(DSO, obj):
    mod_name = obj.__name__
    sz_name = f"get_bitcode_for_{mod_name}_size"
    data_name = f"get_bitcode_for_{mod_name}"
    sz_fptr = getattr(DSO, sz_name)
    sz_fptr.argtypes = ()
    sz_fptr.restype = ctypes.c_long
    data_fptr = getattr(DSO, data_name)
    data_fptr.restype = ctypes.c_void_p
    data_fptr.argtypes = []
    bitcode = bytes((ctypes.c_char * sz_fptr()).from_address(data_fptr()))
    return bitcode


def get_nil(function):
    pass


def specialize(obj):
    def impl(baseline_cpu='host', baseline_features=None):
        if baseline_cpu == 'host':
            from llvmlite import binding as llvm
            target_cpu = llvm.get_host_cpu_name()
        else:
            target_cpu = baseline_cpu
        if baseline_features is None:
            target_features = ()
        else:
            target_features = baseline_features
        print("Specialising library for:", target_cpu, target_features)
        bitcode = obj.__PIXIE__['bitcode']

        from pixie import PIXIECompiler, ExportConfiguration, TranslationUnit
        import os

        module = obj
        outdir = os.path.split(module.__file__)[0]

        export_config = ExportConfiguration('embed_dso')

        tus = (TranslationUnit("self", bitcode),)

        for pysym, variants in obj.__PIXIE__['symbols'].items():
            for sig, data in variants.items():
                export_config.add_symbol(python_name=pysym,
                                         symbol_name=data['symbol'],
                                         signature=sig,)

        print("specialization of", obj, "has uuid", obj.__PIXIE__['uuid'])
        specialized_lib_name = f"{module.__name__}_pixie_specialized"
        lib = PIXIECompiler(library_name=specialized_lib_name,
                            translation_units=tus,
                            export_configuration=export_config,
                            baseline_cpu=target_cpu,
                            baseline_features=target_features,
                            targets_features=(),
                            python_cext=True,
                            # the specialization needs the same UUID
                            uuid=obj.__PIXIE__['uuid'],
                            output_dir=outdir)
        lib.compile()

    return impl


def bootstrap(obj):
    # This is a special function, it acts as the trampoline from builder code
    # that calls this function called "bootstrap" to an actual payload (main).
    # The obj is a reference to the module being initialised, i.e. the PIXIE
    # c-ext lib.
    main(payload, obj)  # noqa: F821. "payload" will be undefined!


def main(PIXIE_payload, obj):
    import importlib
    sbs_name = f'{obj.__name__}_pixie_specialized'
    specialized_mod = None
    try:
        # if a specialized module exists, rewrite this module to look at the
        # specialized one
        specialized_mod = importlib.import_module(sbs_name)
    except ImportError:
        pass

    if specialized_mod is not None:
        # check that uuid of the loading module matches the specialized uuid
        payload_uuid = PIXIE_payload['__PIXIE__']['uuid']
        specialized_uuid = specialized_mod.__PIXIE__['uuid']
        if payload_uuid == specialized_uuid:
            obj.__PIXIE__ = specialized_mod.__PIXIE__
            obj.__PIXIE__['is_specialized'] = True
            msg = (f"\nLoaded specialized module {specialized_mod} in place of"
                   f" {obj}")
            print(msg)
            return
        else:
            import warnings
            # Specialized module UUID doesn't match UUID of loading module
            # warn and load the non-specialized version.
            msg = (f"UUID of specialized module {specialized_mod} does not "
                   f"match that of imported module {obj}, specialization will "
                   "not be used.")
            warnings.warn(msg)

    import ctypes
    DSO = ctypes.CDLL(obj.__file__)
    pixie_dict_raw = PIXIE_payload["__PIXIE__"]
    func_dict = PIXIE_payload["__PIXIE_assemblers__"]
    fns = {}
    for fn in func_dict.keys():
        lcls = {'ctypes': ctypes}
        exec(func_dict[fn], lcls)
        fns[fn] = lcls[fn]

    for sym, overloads in pixie_dict_raw['symbols'].items():
        for sig, data in overloads.items():
            # fix up the ctypes binding on each signature
            ctypes_str = data['ctypes_cfunctype']
            ctbinding = eval(ctypes_str, {'ctypes': ctypes}, {})
            data['ctypes_cfunctype'] = ctbinding
            ct_fptr = fns["address_of_symbol"](DSO, data['symbol'])
            raw_address = ctypes.cast(ct_fptr, ctypes.c_void_p).value
            data['address'] = raw_address
            data['cfunc'] = ctbinding(ct_fptr)
            tmp = dict()
            variants = data['feature_variants']
            # on each variant, find the address in memory and bind a cfunc
            for variant, symbol in variants.items():
                vdict = dict()
                vdict['symbol'] = symbol
                address = fns["address_of_symbol"](DSO, symbol)
                vdict['address'] = address
                vdict['cfunc'] = ctbinding(address)
                tmp[variant] = vdict
            data['feature_variants'] = tmp
    pixie_dict_raw['bitcode'] = fns["get_bitcode"](DSO, obj)
    pixie_dict_raw['specialize'] = fns["specialize"](obj)

    # Write in overlay
    obj.__PIXIE__ = pixie_dict_raw


def add_variant(ctypes_func_string, raw_symbol, baseline,
                feature_variants=None, module=None, source_file=None,
                metadata=None):
    _feature_variants = feature_variants if feature_variants is not None\
        else {}
    d = dict()
    d['ctypes_cfunctype'] = ctypes_func_string
    d['symbol'] = raw_symbol
    d['module'] = module
    d['source_file'] = source_file
    d['address'] = None
    d['cfunc'] = None
    d['feature_variants'] = _feature_variants
    d['baseline_feature'] = baseline
    d['metadata'] = metadata
    return d


def create_base_payload():
    # This is a stub for the "dict" that is present at the root of a PIXIE
    # c-ext
    PIXIE_dunder = dict()
    PIXIE_dunder['symbols'] = {}
    PIXIE_dunder['c_header'] = ['<write it>']
    PIXIE_dunder['linkage'] = None
    PIXIE_dunder['bitcode'] = None  # needs filling in at runtime
    PIXIE_dunder['uuid'] = None
    PIXIE_dunder['is_specialized'] = False

    # These are the functions that are used to populate the PIXIE c-ext module
    # dictionary at runtime.
    PIXIE_assemblers = dict()
    PIXIE_assemblers['main'] = inspect.getsource(main)
    PIXIE_assemblers['get_bitcode'] = inspect.getsource(get_bitcode)
    PIXIE_assemblers['address_of_symbol'] = \
        inspect.getsource(address_of_symbol)
    PIXIE_assemblers['bootstrap'] = inspect.getsource(bootstrap)
    PIXIE_assemblers['specialize'] = inspect.getsource(specialize)

    return {'__PIXIE__': PIXIE_dunder,
            '__PIXIE_assemblers__': PIXIE_assemblers}
