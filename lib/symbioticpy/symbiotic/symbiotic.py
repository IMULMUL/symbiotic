#!/usr/bin/python

import os
import sys
import re

from . options import SymbioticOptions
from . utils import err, dbg, enable_debug, print_elapsed_time, restart_counting_time
from . utils.process import ProcessRunner, runcmd
from . utils.watch import ProcessWatch, DbgWatch
from . utils.utils import print_stdout, print_stderr, get_symbiotic_dir, get_clang_version, required_version
from . exceptions import SymbioticException

class PrepareWatch(ProcessWatch):
    def __init__(self, lines=100):
        ProcessWatch.__init__(self, lines)

    def parse(self, line):
        if b'Removed' in line or b'Defining' in line:
            sys.stdout.write(line.decode('utf-8'))
        else:
            dbg(line.decode('utf-8'), 'prepare', False)


class SlicerWatch(ProcessWatch):
    def __init__(self, lines=100):
        ProcessWatch.__init__(self, lines)

    def parse(self, line):
        if b'INFO' in line:
            dbg(line.decode('utf-8'), domain='slicer', print_nl=False)
        elif b'ERROR' in line or b'error' in line:
            print_stderr(line.decode('utf-8'))
        else:
            dbg(line.decode('utf-8'), 'slicer', False)


class InstrumentationWatch(ProcessWatch):
    def __init__(self, lines=100):
        ProcessWatch.__init__(self, lines)

    def parse(self, line):
        if b'Info' in line:
            dbg(line.decode('utf-8'), domain='instrumentation', print_nl=False)
        elif b'ERROR' in line or b'error' in line:
            print_stderr(line.decode('utf-8'))
        elif b'Inserted' in line:
            print_stdout(line.decode('utf-8'), print_nl=False)
        else:
            dbg(line.decode('utf-8'), 'slicer', False)


class PrintWatch(ProcessWatch):
    def __init__(self, prefix='', color=None):
        ProcessWatch.__init__(self)
        self._prefix = prefix
        self._color = color

    def parse(self, line):
        print_stdout(line.decode('utf-8'), prefix=self._prefix,
                     print_nl=False, color=self._color)


class CompileWatch(ProcessWatch):
    """ Parse output of compilation """

    def __init__(self):
        ProcessWatch.__init__(self)

    def parse(self, line):
        if b'error:' in line:
            print_stderr('cc: {0}'.format(line.decode('utf-8')), color='BROWN')
        else:
            dbg(line.decode('utf-8'), 'compile', print_nl=False)


class UnsuppWatch(ProcessWatch):
    unsupported_call = re.compile('.*call to .* is unsupported.*')

    def __init__(self):
        ProcessWatch.__init__(self)
        self._ok = True

    def ok(self):
        return self._ok

    def parse(self, line):
        uline = line.decode('utf-8')
        dbg(uline, domain='prepare', print_nl=False)
        self._ok = not UnsuppWatch.unsupported_call.match(uline)


class ToolWatch(ProcessWatch):
    def __init__(self, tool):
        # store the whole output of a tool
        ProcessWatch.__init__(self, None)
        self._tool = tool

    def parse(self, line):
        if b'ERROR' in line or b'WARN' in line or b'Assertion' in line\
           or b'error' in line or b'warn' in line:
            sys.stderr.write(line.decode('utf-8'))
        else:
            dbg(line.decode('utf-8'), 'all', False)


def report_results(res):
    dbg(res)
    color = 'BROWN'

    if res.startswith('false'):
        color = 'RED'
        print_stdout('Error found.', color=color)
    elif res == 'true':
        color = 'GREEN'
        print_stdout('No error found.', color=color)
    elif res.startswith('error') or\
            res.startswith('ERROR'):
        color = 'RED'
        print_stdout('Failure!', color=color)

    sys.stdout.flush()
    print_stdout('RESULT: ', print_nl=False)
    print_stdout(res, color=color)
    sys.stdout.flush()

    return res


def get_optlist_before(optlevel):
    from . optimizations import optimizations
    lst = []
    for opt in optlevel:
        if not opt.startswith('before-'):
            continue

        o = opt[7:]
        if o.startswith('opt-'):
            lst.append(o[3:])
        else:
            if o in optimizations:
                lst += optimizations[o]

    return lst


def get_optlist_after(optlevel):
    from . optimizations import optimizations
    lst = []
    for opt in optlevel:
        if not opt.startswith('after-'):
            continue

        o = opt[6:]
        if o.startswith('opt-'):
            lst.append(o[3:])
        else:
            if o in optimizations:
                lst += optimizations[o]

    return lst


class Symbiotic(object):
    """
    Instance of symbiotic tool. Instruments, prepares, compiles and runs
    symbolic execution on given source(s)
    """

    def __init__(self, tool, src, opts=None, symb_dir=None):
        # source file
        self.sources = src
        # source compiled to llvm bytecode
        self.llvmfile = None
        # the file that will be used for symbolic execution
        self.runfile = None
        # the directory that symbiotic script is located
        if symb_dir:
            self.symbiotic_dir = symb_dir
        else:
            self.symbiotic_dir = get_symbiotic_dir()

        if opts is None:
            self.options = SymbioticOptions(self.symbiotic_dir)
        else:
            self.options = opts

        # definitions of our functions that we linked
        self._linked_functions = []

        # tool to use
        self._tool = tool

    def _compile_to_llvm(self, source, output=None, with_g=True, opts=[]):
        """
        Compile given source to LLVM bytecode
        """

        # __inline attribute is buggy in clang, remove it using -D__inline
        cmd = ['clang', '-c', '-emit-llvm', '-include', 'symbiotic.h', '-D__inline='] + opts

        if with_g:
            cmd.append('-g')

        if self.options.CFLAGS:
            cmd += self.options.CFLAGS
        if self.options.CPPFLAGS:
            cmd += self.options.CPPFLAGS

        if self.options.is32bit:
            cmd.append('-m32')

        if self.options.property.memsafety() and required_version(get_clang_version(), "4.0.1"):
            cmd.append('-Xclang')
            cmd.append('-force-lifetime-markers')

        cmd.append('-o')
        if output is None:
            basename = os.path.basename(source)
            llvmfile = '{0}.bc'.format(basename[:basename.rfind('.')])
        else:
            llvmfile = output

        cmd.append(llvmfile)
        cmd.append(source)

        runcmd(cmd, CompileWatch(),
               "Compiling source '{0}' failed".format(source))

        return llvmfile

    def run_opt(self, passes):
        if not passes:
            return

        self._run_opt(passes)

    def _run_opt(self, passes):
        output = '{0}-pr.bc'.format(self.llvmfile[:self.llvmfile.rfind('.')])
        cmd = ['opt', '-load', 'LLVMsbt.so',
               self.llvmfile, '-o', output] + passes

        runcmd(cmd, PrepareWatch(), 'Prepare phase failed')
        self.llvmfile = output

    def _get_stats(self, prefix=''):
        if not self.options.stats:
            return

        cmd = ['opt', '-load', 'LLVMsbt.so', '-count-instr',
               '-o', '/dev/null', self.llvmfile]
        try:
            runcmd(cmd, PrintWatch('INFO: ' + prefix), 'Failed running opt')
        except SymbioticException:
            # not fatal, continue working
            dbg('Failed getting statistics')

    def _instrument(self):
        if not hasattr(self._tool, 'instrumentation_options'):
            return

        config_file, definitions, shouldlink = self._tool.instrumentation_options()
        if config_file is None:
            return

        # if we have config_file, we must have definitions file
        assert definitions

        llvm_dir = 'llvm-{0}'.format(self._tool.llvm_version())
        if self.options.is32bit:
            libdir = os.path.join(self.symbiotic_dir, llvm_dir, 'lib32')
        else:
            libdir = os.path.join(self.symbiotic_dir, llvm_dir, 'lib')

        prefix = self.options.instrumentation_files_path

        definitionsbc = None
        if self.options.property.memsafety():
            # default config file is 'config.json'
            config = prefix + 'memsafety/' + config_file
            assert os.path.isfile(config)
            # check whether we have this file precompiled
            # (this may be a distribution where we're trying to
            # avoid compilation of anything else than sources)
            precompiled_bc = '{0}/{1}.bc'.format(libdir,definitions[:-2])
            if os.path.isfile(precompiled_bc):
                definitionsbc = precompiled_bc
            else:
                definitions = prefix + 'memsafety/{0}'.format(definitions)
                assert os.path.isfile(definitions)
        elif self.options.property.signedoverflow():
            # default config file is 'config.json'
            config = prefix + 'int_overflows/' + config_file
            assert os.path.isfile(config)
            # check whether we have this file precompiled
            # (this may be a distribution where we're trying to
            # avoid compilation of anything else than sources)
            precompiled_bc = '{0}/{1}.bc'.format(libdir,definitions[:-2])
            if os.path.isfile(precompiled_bc):
                definitionsbc = precompiled_bc
            else:
                definitions = prefix + 'int_overflows/{0}'.format(definitions)
                assert os.path.isfile(definitions)
        else:
            raise SymbioticException('BUG: Unhandled property')

        # module with defintions of instrumented functions
        if not definitionsbc:
            definitionsbc = os.path.abspath(self._compile_to_llvm(definitions,\
                 output=os.path.basename(definitions[:-2]+'.bc'),
                 with_g=False, opts=['-O2']))

        assert definitionsbc

        self._get_stats('Before instrumentation ')
        print_stdout('INFO: Starting instrumentation', color='WHITE')

        output = '{0}-inst.bc'.format(self.llvmfile[:self.llvmfile.rfind('.')])
        cmd = ['sbt-instr', config, self.llvmfile, definitionsbc, output]
        if not shouldlink:
            cmd.append('--no-linking')

        restart_counting_time()
        runcmd(cmd, InstrumentationWatch(), 'Instrumenting the code failed')
        print_elapsed_time('INFO: Instrumentation time', color='WHITE')

        self.llvmfile = output
        self._get_stats('After instrumentation ')

    def instrument(self):
        """
        Instrument the code.
        """
        self._instrument()

    def _get_libraries(self, which=[]):
        files = []
        if self.options.add_libc:
            d = '{0}/lib'.format(self.symbiotic_dir)
            if self.options.is32bit:
                d += '32'

            files.append('{0}/klee/runtime/klee-libc.bc'.format(d))

        return files

    def link(self, output=None, libs=None):
        if libs is None:
            libs = self._get_libraries()

        if not libs:
            return

        if output is None:
            output = '{0}-ln.bc'.format(
                self.llvmfile[:self.llvmfile.rfind('.')])

        cmd = ['llvm-link', '-o', output] + libs
        if self.llvmfile:
            cmd.append(self.llvmfile)

        runcmd(cmd, DbgWatch('compile'),
               'Failed linking llvm file with libraries')
        self.llvmfile = output

    def _link_undefined(self, undefs):
        def get_path(symbdir, ty, tool, name):
            path = os.path.abspath('{0}/lib/{1}/{2}/{3}.c'.format(symbdir, ty, tool, undef))
            if os.path.isfile(path):
                return path

            # do we have at least a generic implementation?
            path = os.path.abspath('{0}/lib/{1}/{2}.c'.format(symbdir, ty, undef))
            if os.path.isfile(path):
                return path

            return None

        tolink = []
        for ty in self.options.linkundef:
            for undef in undefs:
                path = get_path(self.symbiotic_dir, ty,
                                self._tool.name(), undef)
                if path is None:
                    continue

                output = os.path.abspath('{0}.bc'.format(os.path.basename(path)[:-2]))
                self._compile_to_llvm(path, output)
                tolink.append(output)

                # for debugging
                self._linked_functions.append(undef)

        if tolink:
            self.link(libs=tolink)
            return True

        return False

    def link_unconditional(self):
        """ Link the files that we got on the command line """

        return self._link_undefined(self.options.link_files)

    def _get_undefined(self, bitcode, only_func=[]):
        cmd = ['llvm-nm', '-undefined-only', '-just-symbol-name', bitcode]
        watch = ProcessWatch(None)
        runcmd(cmd, watch, 'Failed getting undefined symbols from bitcode')
        undefs = map(lambda s: s.strip(), watch.getLines())
        if only_func:
            return filter(set(only_func).__contains__, undefs)
        return undefs

    def link_undefined(self, only_func=[]):
        if not self.options.linkundef:
            return

        # get undefined functions from the bitcode
        undefs = self._get_undefined(self.llvmfile, only_func)
        if self._link_undefined([x.decode('ascii') for x in undefs]):
            # if we linked someting, try get undefined again,
            # because the functions may have added some new undefined
            # functions
            if not only_func:
                self.link_undefined()

    def slicer(self, add_params=[]):
        if hasattr(self._tool, 'slicer_options'):
            crit, opts = self._tool.slicer_options()
        else:
            crit, opts = '__assert_fail,__VERIFIER_error', []

        output = '{0}.sliced'.format(self.llvmfile[:self.llvmfile.rfind('.')])
        cmd = ['sbt-slicer', '-c', crit] + opts
        if self.options.slicer_pta in ['fi', 'fs']:
            cmd.append('-pta')
            cmd.append(self.options.slicer_pta)

        # we do that now using _get_stats
        # cmd.append('-statistics')

        if self.options.undefined_are_pure:
            cmd.append('-undefined-are-pure')

        if self.options.slicer_params:
            cmd += self.options.slicer_params

        if add_params:
            cmd += add_params

        cmd.append(self.llvmfile)

        runcmd(cmd, SlicerWatch(), 'Slicing failed')
        self.llvmfile = output

    def optimize(self, passes, disable=[]):
        if self.options.no_optimize:
            return

        disable += self.options.disabled_optimizations
        if disable:
            ds = set(disable)
            passes = filter(lambda x: not ds.__contains__(x), passes)

        if not passes:
            dbg("No passes available for optimizations")

        output = '{0}-opt.bc'.format(self.llvmfile[:self.llvmfile.rfind('.')])
        cmd = ['opt', '-o', output, self.llvmfile]
        cmd += passes

        restart_counting_time()
        runcmd(cmd, CompileWatch(), 'Optimizing the code failed')
        print_elapsed_time('INFO: Optimizations time', color='WHITE')

        self.llvmfile = output

    def check_llvmfile(self, llvmfile, check='-check-unsupported'):
        """
        Check whether the bitcode does not contain anything
        that we do not support
        """
        cmd = ['opt', '-load', 'LLVMsbt.so', check,
               '-o', '/dev/null', llvmfile]
        try:
            runcmd(cmd, UnsuppWatch(), 'Failed checking the code')
        except SymbioticException:
            return False

        return True

    def postprocess_llvm(self):
        """
        Run a command that proprocesses the llvm code
        for a particular tool
        """
        if not hasattr(self._tool, 'postprocess_llvm'):
            return

        cmd, output = self._tool.postprocess_llvm(self.llvmfile)
        if not cmd:
            return

        runcmd(cmd, DbgWatch('compile'),
                  'Failed preprocessing the llvm code')
        self.llvmfile = output

    def get_klee_functions(self, bitcode):
        """ Check whether the code contains any KLEE functions """
        kf = []
        for f in self._get_undefined(bitcode):
            if f.startswith('klee_'):
                kf.append(f)
        return kf

    def run_verification(self):
        cmd = self._tool.cmdline(self._tool.executable(),
                                 self.options.tool_params, [self.llvmfile],
                                 self.options.property.getPrpFile(), [])

        returncode = 0
        watch = ToolWatch(self._tool)
        try:
            runcmd(cmd, watch, 'Running the verifier failed')
        except SymbioticException as e:
            print_stderr(str(e), color='RED')
            returncode = 1

        return self._tool.determine_result(returncode, 0,
                                           watch.getLines(), False)

    def terminate(self):
        pr = ProcessRunner()
        if pr.hasProcess():
            pr.terminate()

    def kill(self):
        pr = ProcessRunner()
        if pr.hasProcess():
            pr.kill()

    def kill_wait(self):
        pr = ProcessRunner()
        if not pr.hasProcess():
            return

        if pr.exitStatus() is None:
            from time import sleep
            while pr.exitStatus() is None:
                pr.kill()

                print('Waiting for the child process to terminate')
                sleep(0.5)

            print('Killed the child process')

    def run(self):
        try:
            return self._run_symbiotic()
        except KeyboardInterrupt:
            self.terminate()
            self.kill()
            print('Interrupted...')

    def _compile_sources(self):
        llvmsrc = []
        for source in self.sources:
            opts = ['-Wno-unused-parameter', '-Wno-unused-attribute',
                    '-Wno-unused-label', '-Wno-unknown-pragmas']
            if hasattr(self._tool, 'compilation_options'):
                opts += self._tool.compilation_options()

            if self.options.property.signedoverflow():
                # FIXME: this is a hack, remove once we have better CD algorithm
                self.options.disabled_optimizations = ['-instcombine']

            llvms = self._compile_to_llvm(source, opts=opts)
            llvmsrc.append(llvms)

        # link all compiled sources to a one bytecode
        # the result is stored to self.llvmfile
        self.link('code.bc', llvmsrc)

    def perform_slicing(self):
        # run optimizations that can make slicing more precise
        opt = get_optlist_before(self.options.optlevel)
        if opt:
            self.optimize(passes=opt)

        # Break the infinite loops just before slicing so that the
        # optimizations won't make them syntactically infinite again. We must
        # run reg2mem before breaking to loops, because breaking the loops can
        # not handle PHI nodes well.
        self.run_opt(['-reg2mem', '-break-infinite-loops',
                      '-remove-infinite-loops',
                      '-mem2reg'
                      ])

        self._get_stats('Before slicing ')

        print_stdout('INFO: Starting slicing', color='WHITE')
        restart_counting_time()
        for n in range(0, self.options.repeat_slicing):
            dbg('Slicing the code for the {0}. time'.format(n + 1))
            add_params = []
            # if n == 0 and self.options.repeat_slicing > 1:
            #    add_params = ['-pta-field-sensitive=8']

            self.slicer(add_params)

            if self.options.repeat_slicing > 1:
                opt = get_optlist_after(self.options.optlevel)
                if opt:
                    self.optimize(passes=opt)
                    self.run_opt(['-break-infinite-loops',
                                  '-remove-infinite-loops'])

        print_elapsed_time('INFO: Total slicing time', color='WHITE')

        self._get_stats('After slicing ')

    def _disable_some_optimizations(self, llvm_version):
        # disable optimizations that are not in particular llvm versions
        ver_major, ver_minor, ver_micro = map(int, llvm_version.split('.'))
        disabled = []
        if ver_major != 3:
            return []

        if ver_minor <= 7:
            disabled += ['-aa', '-demanded-bits',
                        '-globals-aa', '-forceattrs',
                        '-inferattrs', '-rpo-functionattrs']
        if ver_minor <= 6:
            disabled += [ '-tti', '-bdce', '-elim-avail-extern',
                          '-float2int', '-loop-accesses']

        if disabled:
            dbg('Disabled these optimizations: {0}'.format(str(disabled)))
        self.options.disabled_optimizations = disabled

    def _run_symbiotic(self):
        restart_counting_time()

        dbg('Running Symbiotic with {0}'.format(self._tool.name()))

        self._disable_some_optimizations(self._tool.llvm_version())

        #################### #################### ###################
        # COMPILATION
        #  - compile the code into LLVM bitcode
        #################### #################### ###################

        # compile all sources if the file is not given
        # as a .bc file
        if self.options.source_is_bc:
            self.llvmfile = self.sources[0]
        else:
            self._compile_sources()

        # make the path absolute
        self.llvmfile = os.path.abspath(self.llvmfile)

        self._get_stats('After compilation ')

        # FIXME: make tool-specific
        if not self.check_llvmfile(self.llvmfile, '-check-concurr'):
            print('Unsupported call (probably pthread API or floating point stdlib functions)')
            return report_results('unknown')

        # link the files that we got on the command line
        # and that we are required to link in on any circumstances
        self.link_unconditional()

        passes = []
        if self.options.property.memsafety() or \
           self.options.property.undefinedness() or \
           self.options.property.signedoverflow():
            # remove the original calls to __VERIFIER_error
            passes.append('-remove-error-calls')
        if hasattr(self._tool, 'passes_after_compilation'):
            passes += self._tool.passes_after_compilation()

        if self.options.property.signedoverflow():
            passes.append('-mem2reg')
            passes.append('-break-crit-edges')

        self.run_opt(passes)

        # we want to link memsafety functions before instrumentation,
        # because we need to check for invalid dereferences in them
        if self.options.property.memsafety():
            self.link_undefined()

        if self.options.property.signedoverflow():
			self.link_undefined()

        #################### #################### ###################
        # INSTRUMENTATION
        #  - now instrument the code according to the given property
        #################### #################### ###################
        self.instrument()

        if hasattr(self._tool, 'passes_after_instrumentation'):
            passes = self._tool.passes_after_instrumentation()
            self.run_opt(passes)

        # link with the rest of libraries if needed (klee-libc)
        self.link()

        # link undefined (no-op when prepare is turned off)
        # (this still can have an effect even in memsafety, since we
        # can link __VERIFIER_malloc0.c or similar)
        self.link_undefined()

        #################### #################### ###################
        # SLICING
        #  - slice the code w.r.t error sites
        #################### #################### ###################
        if not self.options.noslice:
            self.perform_slicing()

        # start a new time era
        restart_counting_time()

        # optimize the code after slicing and before verification
        opt = get_optlist_after(self.options.optlevel)
        if opt:
            self.optimize(passes=opt)


        # there may have been created new loops
        passes = ['-remove-infinite-loops']
        if hasattr(self._tool, 'passes_after_slicing'):
            passes += self._tool.passes_after_slicing()
        self.run_opt(passes)

        # FIXME: move these checks to tool specific code
        if self._tool.name() == 'klee' and not self.check_llvmfile(self.llvmfile):
            dbg('Unsupported call (probably floating handling)')
            return report_results('unsupported call')

        # delete-undefined may insert __VERIFIER_make_nondet
        # and also other funs like __errno_location may be included
        self.link_undefined()

        if self._linked_functions:
            print('Linked our definitions to these undefined functions:')
            for f in self._linked_functions:
                print_stdout('  ', print_nl=False)
                print_stdout(f)

        # XXX: we could optimize the code again here...
        print_elapsed_time('INFO: After-slicing optimizations and transformations time',
                           color='WHITE')

        # check that if we do not use KLEE, we do not have any klee functions in the code
        if self._tool.name() != "klee":
            kf = self.get_klee_functions(self.llvmfile)
            if kf:
                raise SymbioticException('Code contains KLEE functions, but the verifier is not KLEE ({0})'.format(' '.join(kf)))

        # tool's specific preprocessing steps
        self.postprocess_llvm()

        if not self.options.final_output is None:
            # copy the file to final_output
            try:
                os.rename(self.llvmfile, self.options.final_output)
                self.llvmfile = self.options.final_output
            except OSError as e:
                msg = 'Cannot create {0}: {1}'.format(
                    self.options.final_output, e.message)
                raise SymbioticException(msg)

        #################### #################### ###################
        # VERIFICATION
        #  - run the verification backend
        #################### #################### ###################
        if not self.options.no_verification:
            self._get_stats('Before verification ')
            print_stdout('INFO: Starting verification', color='WHITE')

            restart_counting_time()
            found = self.run_verification()

            print_elapsed_time('INFO: Verification time', color='WHITE')
        else:
            found = 'Did not run verification'

        return report_results(found)
