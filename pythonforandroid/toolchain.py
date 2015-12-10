#!/usr/bin/env python
"""
Tool for compiling Android toolchain
====================================

This tool intend to replace all the previous tools/ in shell script.
"""

from __future__ import print_function

import sys
from sys import stdout, stderr, platform
from os.path import (join, dirname, realpath, exists, isdir, basename,
                     expanduser, splitext, split)
from os import listdir, unlink, makedirs, environ, chdir, getcwd, uname
import os
import zipfile
import tarfile
import importlib
import io
import json
import glob
import shutil
import re
import imp
import contextlib
import logging
import shlex
from copy import deepcopy
from functools import wraps
from datetime import datetime
from distutils.spawn import find_executable
from tempfile import mkdtemp
from math import log10
try:
    from urllib.request import FancyURLopener
except ImportError:
    from urllib import FancyURLopener
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

import argparse
from appdirs import user_data_dir
import sh


from pythonforandroid.archs import ArchARM, ArchARMv7_a, Archx86, Archx86_64
from pythonforandroid.recipebases import (Recipe, NDKRecipe, IncludedFilesBehaviour,
                         PythonRecipe, CythonRecipe,
                         CompiledComponentsPythonRecipe)
from pythonforandroid.logger import (logger, info, debug, warning, error,
                                     Out_Style, Out_Fore, Err_Style, Err_Fore)

# monkey patch to show full output
sh.ErrorReturnCode.truncate_cap = 999999


user_dir = dirname(realpath(os.path.curdir))
toolchain_dir = dirname(__file__)
sys.path.insert(0, join(toolchain_dir, "tools", "external"))


DEFAULT_ANDROID_API = 15

IS_PY3 = sys.version_info[0] >= 3

info(''.join(
    [Err_Style.BRIGHT, Err_Fore.RED,
     'This python-for-android revamp is an experimental alpha release!',
     Err_Style.RESET_ALL]))
info(''.join(
    [Err_Fore.RED,
     ('It should work (mostly), but you may experience '
      'missing features or bugs.'),
     Err_Style.RESET_ALL]))


def info_main(*args):
    logger.info(''.join([Err_Style.BRIGHT, Err_Fore.GREEN] + list(args) +
                        [Err_Style.RESET_ALL, Err_Fore.RESET]))


def info_notify(s):
    info('{}{}{}{}'.format(Err_Style.BRIGHT, Err_Fore.LIGHTBLUE_EX, s,
                           Err_Style.RESET_ALL))


def pretty_log_dists(dists, log_func=info):
    infos = []
    for dist in dists:
        infos.append('{Fore.GREEN}{Style.BRIGHT}{name}{Style.RESET_ALL}: '
                     'includes recipes ({Fore.GREEN}{recipes}'
                     '{Style.RESET_ALL}), built for archs ({Fore.BLUE}'
                     '{archs}{Style.RESET_ALL})'.format(
                         name=dist.name, recipes=', '.join(dist.recipes),
                         archs=', '.join(dist.archs) if dist.archs else 'UNKNOWN',
                         Fore=Err_Fore, Style=Err_Style))

    for line in infos:
        log_func('\t' + line)


def shorten_string(string, max_width):
    ''' make limited length string in form:
      "the string is very lo...(and 15 more)"
    '''
    string_len = len(string)
    if string_len <= max_width:
        return string
    visible = max_width - 16 - int(log10(string_len))
    # expected suffix len "...(and XXXXX more)"
    return ''.join((string[:visible], '...(and ', str(string_len - visible),
                    ' more)'))


def shprint(command, *args, **kwargs):
    '''Runs the command (which should be an sh.Command instance), while
    logging the output.'''
    kwargs["_iter"] = True
    kwargs["_out_bufsize"] = 1
    kwargs["_err_to_out"] = True
    kwargs["_bg"] = True
    is_critical = kwargs.pop('_critical', False)
    tail_n = kwargs.pop('_tail', 0)
    filter_in = kwargs.pop('_filter', None)
    filter_out = kwargs.pop('_filterout', None)
    if len(logger.handlers) > 1:
        logger.removeHandler(logger.handlers[1])
    try:
        columns = max(25, int(os.popen('stty size', 'r').read().split()[1]))
    except:
        columns = 100
    command_path = str(command).split('/')
    command_string = command_path[-1]
    string = ' '.join(['running', command_string] + list(args))

    # If logging is not in DEBUG mode, trim the command if necessary
    if logger.level > logging.DEBUG:
        logger.info('{}{}'.format(shorten_string(string, columns - 12),
                                  Err_Style.RESET_ALL))
    else:
        logger.debug('{}{}'.format(string, Err_Style.RESET_ALL))

    need_closing_newline = False
    try:
        msg_hdr = '           working: '
        msg_width = columns - len(msg_hdr) - 1
        output = command(*args, **kwargs)
        for line in output:
            if logger.level > logging.DEBUG:
                msg = line.replace(
                    '\n', ' ').replace(
                        '\t', ' ').replace(
                            '\b', ' ').rstrip()
                if msg:
                    sys.stdout.write(u'{}\r{}{:<{width}}'.format(
                        Err_Style.RESET_ALL, msg_hdr,
                        shorten_string(msg, msg_width), width=msg_width))
                    sys.stdout.flush()
                    need_closing_newline = True
            else:
                logger.debug(''.join(['\t', line.rstrip()]))
        if need_closing_newline:
            sys.stdout.write('{}\r{:>{width}}\r'.format(
                Err_Style.RESET_ALL, ' ', width=(columns - 1)))
            sys.stdout.flush()
    except sh.ErrorReturnCode as err:
        if need_closing_newline:
            sys.stdout.write('{}\r{:>{width}}\r'.format(
                Err_Style.RESET_ALL, ' ', width=(columns - 1)))
            sys.stdout.flush()
        if tail_n or filter_in or filter_out:
            def printtail(out, name, forecolor, tail_n=0,
                          re_filter_in=None, re_filter_out=None):
                lines = out.splitlines()
                if re_filter_in is not None:
                    lines = [l for l in lines if re_filter_in.search(l)]
                if re_filter_out is not None:
                    lines = [l for l in lines if not re_filter_out.search(l)]
                if tail_n == 0 or len(lines) <= tail_n:
                    info('{}:\n{}\t{}{}'.format(
                        name, forecolor, '\t\n'.join(lines), Out_Fore.RESET))
                else:
                    info('{} (last {} lines of {}):\n{}\t{}{}'.format(
                        name, tail_n, len(lines),
                        forecolor, '\t\n'.join(lines[-tail_n:]), Out_Fore.RESET))
            printtail(err.stdout, 'STDOUT', Out_Fore.YELLOW, tail_n,
                      re.compile(filter_in) if filter_in else None,
                      re.compile(filter_out) if filter_out else None)
            printtail(err.stderr, 'STDERR', Err_Fore.RED)
        if is_critical:
            env = kwargs.get("env")
            if env is not None:
                info("{}ENV:{}\n{}\n".format(
                    Err_Fore.YELLOW, Err_Fore.RESET, "\n".join(
                        "set {}={}".format(n, v) for n, v in env.items())))
            info("{}COMMAND:{}\ncd {} && {} {}\n".format(
                Err_Fore.YELLOW, Err_Fore.RESET, getcwd(), command, ' '.join(args)))
            warning("{}ERROR: {} failed!{}".format(
                Err_Fore.RED, command, Err_Fore.RESET))
            exit(1)
        else:
            raise

    return output

# shprint(sh.ls, '-lah')
# exit(1)


def add_boolean_option(parser, names, no_names=None,
                       default=True, dest=None, description=None):
    group = parser.add_argument_group(description=description)
    if not isinstance(names, (list, tuple)):
        names = [names]
    if dest is None:
        dest = names[0].strip("-").replace("-", "_")

    def add_dashes(x):
        return x if x.startswith("-") else "--"+x

    opts = [add_dashes(x) for x in names]
    group.add_argument(
        *opts, help=("(this is the default)" if default else None),
        dest=dest, action='store_true')
    if no_names is None:
        def add_no(x):
            x = x.lstrip("-")
            return ("no_"+x) if "_" in x else ("no-"+x)
        no_names = [add_no(x) for x in names]
    opts = [add_dashes(x) for x in no_names]
    group.add_argument(
        *opts, help=(None if default else "(this is the default)"),
        dest=dest, action='store_false')
    parser.set_defaults(**{dest: default})


def require_prebuilt_dist(func):
    '''Decorator for ToolchainCL methods. If present, the method will
    automatically make sure a dist has been built before continuing
    or, if no dists are present or can be obtained, will raise an
    error.

    '''

    @wraps(func)
    def wrapper_func(self, args):
        ctx = self.ctx
        ctx.set_archs(self._archs)
        ctx.prepare_build_environment(user_sdk_dir=self.sdk_dir,
                                      user_ndk_dir=self.ndk_dir,
                                      user_android_api=self.android_api,
                                      user_ndk_ver=self.ndk_version)
        dist = self._dist
        if dist.needs_build:
            info_notify('No dist exists that meets your requirements, '
                        'so one will be built.')
            args = build_dist_from_args(ctx, dist, args)
        func(self, args)
    return wrapper_func


def get_directory(filename):
    '''If the filename ends with a recognised file extension, return the
    filename without this extension.'''
    if filename.endswith('.tar.gz'):
        return basename(filename[:-7])
    elif filename.endswith('.tgz'):
        return basename(filename[:-4])
    elif filename.endswith('.tar.bz2'):
        return basename(filename[:-8])
    elif filename.endswith('.tbz2'):
        return basename(filename[:-5])
    elif filename.endswith('.zip'):
        return basename(filename[:-4])
    info('Unknown file extension for {}'.format(filename))
    exit(1)


def which(program, path_env):
    '''Locate an executable in the system.'''
    import os

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in path_env.split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None


@contextlib.contextmanager
def current_directory(new_dir):
    cur_dir = getcwd()
    logger.info(''.join((Err_Fore.CYAN, '-> directory context ', new_dir,
                         Err_Fore.RESET)))
    chdir(new_dir)
    yield
    logger.info(''.join((Err_Fore.CYAN, '<- directory context ', cur_dir,
                         Err_Fore.RESET)))
    chdir(cur_dir)


@contextlib.contextmanager
def temp_directory():
    temp_dir = mkdtemp()
    try:
        logger.debug(''.join((Err_Fore.CYAN, ' + temp directory used ',
                              temp_dir, Err_Fore.RESET)))
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)
        logger.debug(''.join((Err_Fore.CYAN, ' - temp directory deleted ',
                              temp_dir, Err_Fore.RESET)))


def cache_execution(f):
    def _cache_execution(self, *args, **kwargs):
        state = self.ctx.state
        key = "{}.{}".format(self.name, f.__name__)
        force = kwargs.pop("force", False)
        if args:
            for arg in args:
                key += ".{}".format(arg)
        key_time = "{}.at".format(key)
        if key in state and not force:
            print("# (ignored) {} {}".format(
                f.__name__.capitalize(), self.name))
            return
        print("{} {}".format(f.__name__.capitalize(), self.name))
        f(self, *args, **kwargs)
        state[key] = True
        state[key_time] = str(datetime.utcnow())
    return _cache_execution


class ChromeDownloader(FancyURLopener):
    version = (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/28.0.1500.71 Safari/537.36')

urlretrieve = ChromeDownloader().retrieve


class JsonStore(object):
    """Replacement of shelve using json, needed for support python 2 and 3.
    """

    def __init__(self, filename):
        super(JsonStore, self).__init__()
        self.filename = filename
        self.data = {}
        if exists(filename):
            try:
                with io.open(filename, encoding='utf-8') as fd:
                    self.data = json.load(fd)
            except ValueError:
                print("Unable to read the state.db, content will be replaced.")

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.sync()

    def __delitem__(self, key):
        del self.data[key]
        self.sync()

    def __contains__(self, item):
        return item in self.data

    def get(self, item, default=None):
        return self.data.get(item, default)

    def keys(self):
        return self.data.keys()

    def remove_all(self, prefix):
        for key in self.data.keys()[:]:
            if not key.startswith(prefix):
                continue
            del self.data[key]
        self.sync()

    def sync(self):
        # http://stackoverflow.com/questions/12309269/write-json-data-to-file-in-python/14870531#14870531
        if IS_PY3:
            with open(self.filename, 'w') as fd:
                json.dump(self.data, fd, ensure_ascii=False)
        else:
            with io.open(self.filename, 'w', encoding='utf-8') as fd:
                fd.write(unicode(json.dumps(self.data, ensure_ascii=False)))


class Graph(object):
    # Taken from the old python-for-android/depsort
    # Modified to include alternative dependencies
    def __init__(self):
        # `graph`: dict that maps each package to a set of its dependencies.
        self.graphs = [{}]
        # self.graph = {}

    def remove_redundant_graphs(self):
        '''Removes possible graphs if they are equivalent to others.'''
        graphs = self.graphs
        # Walk the list backwards so that popping elements doesn't
        # mess up indexing.

        # n.b. no need to test graph 0 as it will have been tested against
        # all others by the time we get to it
        for i in range(len(graphs) - 1, 0, -1):   
            graph = graphs[i]

            #test graph i against all graphs 0 to i-1
            for j in range(0, i):
                comparison_graph = graphs[j]
                
                if set(comparison_graph.keys()) == set(graph.keys()):
                    #graph[i] == graph[j]
                    #so remove graph[i] and continue on to testing graph[i-1]
                    graphs.pop(i)
                    break

    def add(self, dependent, dependency):
        """Add a dependency relationship to the graph"""
        if isinstance(dependency, (tuple, list)):
            for graph in self.graphs[:]:
                for dep in dependency[1:]:
                    new_graph = deepcopy(graph)
                    self._add(new_graph, dependent, dep)
                    self.graphs.append(new_graph)
                self._add(graph, dependent, dependency[0])
        else:
            for graph in self.graphs:
                self._add(graph, dependent, dependency)
        self.remove_redundant_graphs()

    def _add(self, graph, dependent, dependency):
        '''Add a dependency relationship to a specific graph, where dependency
        must be a single dependency, not a list or tuple.
        '''
        graph.setdefault(dependent, set())
        graph.setdefault(dependency, set())
        if dependent != dependency:
            graph[dependent].add(dependency)

    def conflicts(self, conflict):
        graphs = self.graphs
        for i in range(len(graphs)):
            graph = graphs[len(graphs) - 1 - i]
            if conflict in graph:
                graphs.pop(len(graphs) - 1 - i)
        return len(graphs) == 0

    def remove_remaining_conflicts(self, ctx):
        # It's unpleasant to have to pass ctx as an argument...
        '''Checks all possible graphs for conflicts that have arisen during
        the additon of alternative repice branches, as these are not checked
        for conflicts at the time.'''
        new_graphs = []
        for i, graph in enumerate(self.graphs):
            for name in graph.keys():
                recipe = Recipe.get_recipe(name, ctx)
                if any([c in graph for c in recipe.conflicts]):
                    break
            else:
                new_graphs.append(graph)
        self.graphs = new_graphs

    def add_optional(self, dependent, dependency):
        """Add an optional (ordering only) dependency relationship to the graph

        Only call this after all mandatory requirements are added
        """
        for graph in self.graphs:
            if dependent in graph and dependency in graph:
                self._add(graph, dependent, dependency)

    def find_order(self, index=0):
        """Do a topological sort on a dependency graph

        :Parameters:
            :Returns:
                iterator, sorted items form first to last
        """
        graph = self.graphs[index]
        graph = dict((k, set(v)) for k, v in graph.items())
        while graph:
            # Find all items without a parent
            leftmost = [l for l, s in graph.items() if not s]
            if not leftmost:
                raise ValueError('Dependency cycle detected! %s' % graph)
            # If there is more than one, sort them for predictable order
            leftmost.sort()
            for result in leftmost:
                # Yield and remove them from the graph
                yield result
                graph.pop(result)
                for bset in graph.values():
                    bset.discard(result)


class Context(object):
    '''A build context. If anything will be built, an instance this class
    will be instantiated and used to hold all the build state.'''

    env = environ.copy()
    root_dir = None     # the filepath of toolchain.py
    storage_dir = None  # the root dir where builds and dists will be stored

    build_dir = None  # in which bootstraps are copied for building and recipes are built
    dist_dir = None  # the Android project folder where everything ends up
    libs_dir = None  # where Android libs are cached after build but
                     # before being placed in dists
    aars_dir = None
    javaclass_dir = None

    ccache = None  # whether to use ccache
    cython = None  # the cython interpreter name

    ndk_platform = None  # the ndk platform directory

    dist_name = None  # should be deprecated in favour of self.dist.dist_name
    bootstrap = None
    bootstrap_build_dir = None

    recipe_build_order = None  # Will hold the list of all built recipes

    @property
    def packages_path(self):
        '''Where packages are downloaded before being unpacked'''
        return join(self.storage_dir, 'packages')

    @property
    def templates_dir(self):
        return join(self.root_dir, 'templates')

    @property
    def libs_dir(self):
        # Was previously hardcoded as self.build_dir/libs
        dir = join(self.build_dir, 'libs_collections',
                   self.bootstrap.distribution.name)
        ensure_dir(dir)
        return dir

    @property
    def javaclass_dir(self):
        # Was previously hardcoded as self.build_dir/java
        dir = join(self.build_dir, 'javaclasses',
                   self.bootstrap.distribution.name)
        ensure_dir(dir)
        return dir

    @property
    def aars_dir(self):
        dir = join(self.build_dir, 'aars', self.bootstrap.distribution.name)
        ensure_dir(dir)
        return dir

    @property
    def python_installs_dir(self):
        dir = join(self.build_dir, 'python-installs')
        ensure_dir(dir)
        return dir

    def get_python_install_dir(self):
        dir = join(self.python_installs_dir, self.bootstrap.distribution.name)
        return dir

    def setup_dirs(self):
        '''Calculates all the storage and build dirs, and makes sure
        the directories exist where necessary.'''
        self.root_dir = realpath(dirname(__file__))

        # AND: TODO: Allow the user to set the build_dir
        self.storage_dir = user_data_dir('python-for-android')
        self.build_dir = join(self.storage_dir, 'build')
        self.dist_dir = join(self.storage_dir, 'dists')

        ensure_dir(self.storage_dir)
        ensure_dir(self.build_dir)
        ensure_dir(self.dist_dir)

    @property
    def android_api(self):
        '''The Android API being targeted.'''
        if self._android_api is None:
            raise ValueError('Tried to access android_api but it has not '
                             'been set - this should not happen, something '
                             'went wrong!')
        return self._android_api

    @android_api.setter
    def android_api(self, value):
        self._android_api = value

    @property
    def ndk_ver(self):
        '''The version of the NDK being used for compilation.'''
        if self._ndk_ver is None:
            raise ValueError('Tried to access android_api but it has not '
                             'been set - this should not happen, something '
                             'went wrong!')
        return self._ndk_ver

    @ndk_ver.setter
    def ndk_ver(self, value):
        self._ndk_ver = value

    @property
    def sdk_dir(self):
        '''The path to the Android SDK.'''
        if self._sdk_dir is None:
            raise ValueError('Tried to access android_api but it has not '
                             'been set - this should not happen, something '
                             'went wrong!')
        return self._sdk_dir

    @sdk_dir.setter
    def sdk_dir(self, value):
        self._sdk_dir = value

    @property
    def ndk_dir(self):
        '''The path to the Android NDK.'''
        if self._ndk_dir is None:
            raise ValueError('Tried to access android_api but it has not '
                             'been set - this should not happen, something '
                             'went wrong!')
        return self._ndk_dir

    @ndk_dir.setter
    def ndk_dir(self, value):
        self._ndk_dir = value

    def prepare_build_environment(self, user_sdk_dir, user_ndk_dir,
                                  user_android_api, user_ndk_ver):
        '''Checks that build dependencies exist and sets internal variables
        for the Android SDK etc.

        ..warning:: This *must* be called before trying any build stuff

        '''

        if self._build_env_prepared:
            return

        # AND: This needs revamping to carefully check each dependency
        # in turn
        ok = True

        # Work out where the Android SDK is
        sdk_dir = None
        if user_sdk_dir:
            sdk_dir = user_sdk_dir
        if sdk_dir is None:  # This is the old P4A-specific var
            sdk_dir = environ.get('ANDROIDSDK', None)
        if sdk_dir is None:  # This seems used more conventionally
            sdk_dir = environ.get('ANDROID_HOME', None)
        if sdk_dir is None:  # Checks in the buildozer SDK dir, useful
            #                # for debug tests of p4a
            possible_dirs = glob.glob(expanduser(join(
                '~', '.buildozer', 'android', 'platform', 'android-sdk-*')))
            if possible_dirs:
                info('Found possible SDK dirs in buildozer dir: {}'.format(
                    ', '.join([d.split(os.sep)[-1] for d in possible_dirs])))
                info('Will attempt to use SDK at {}'.format(possible_dirs[0]))
                warning('This SDK lookup is intended for debug only, if you '
                        'use python-for-android much you should probably '
                        'maintain your own SDK download.')
                sdk_dir = possible_dirs[0]
        if sdk_dir is None:
            warning('Android SDK dir was not specified, exiting.')
            exit(1)
        self.sdk_dir = realpath(sdk_dir)

        # Check what Android API we're using
        android_api = None
        if user_android_api:
            android_api = user_android_api
            if android_api is not None:
                info('Getting Android API version from user argument')
        if android_api is None:
            android_api = environ.get('ANDROIDAPI', None)
            if android_api is not None:
                info('Found Android API target in $ANDROIDAPI')
        if android_api is None:
            info('Android API target was not set manually, using '
                 'the default of {}'.format(DEFAULT_ANDROID_API))
            android_api = DEFAULT_ANDROID_API
        android_api = int(android_api)
        self.android_api = android_api

        android = sh.Command(join(sdk_dir, 'tools', 'android'))
        targets = android('list').stdout.split('\n')
        apis = [s for s in targets if re.match(r'^ *API level: ', s)]
        apis = [re.findall(r'[0-9]+', s) for s in apis]
        apis = [int(s[0]) for s in apis if s]
        info('Available Android APIs are ({})'.format(
            ', '.join(map(str, apis))))
        if android_api in apis:
            info(('Requested API target {} is available, '
                  'continuing.').format(android_api))
        else:
            warning(('Requested API target {} is not available, install '
                     'it with the SDK android tool.').format(android_api))
            warning('Exiting.')
            exit(1)

        # Find the Android NDK
        # Could also use ANDROID_NDK, but doesn't look like many tools use this
        ndk_dir = None
        if user_ndk_dir:
            ndk_dir = user_ndk_dir
            if ndk_dir is not None:
                info('Getting NDK dir from from user argument')
        if ndk_dir is None:  # The old P4A-specific dir
            ndk_dir = environ.get('ANDROIDNDK', None)
            if ndk_dir is not None:
                info('Found NDK dir in $ANDROIDNDK')
        if ndk_dir is None:  # Apparently the most common convention
            ndk_dir = environ.get('NDK_HOME', None)
            if ndk_dir is not None:
                info('Found NDK dir in $NDK_HOME')
        if ndk_dir is None:  # Another convention (with maven?)
            ndk_dir = environ.get('ANDROID_NDK_HOME', None)
            if ndk_dir is not None:
                info('Found NDK dir in $ANDROID_NDK_HOME')
        if ndk_dir is None:  # Checks in the buildozer NDK dir, useful
            #                # for debug tests of p4a
            possible_dirs = glob.glob(expanduser(join(
                '~', '.buildozer', 'android', 'platform', 'android-ndk-r*')))
            if possible_dirs:
                info('Found possible NDK dirs in buildozer dir: {}'.format(
                    ', '.join([d.split(os.sep)[-1] for d in possible_dirs])))
                info('Will attempt to use NDK at {}'.format(possible_dirs[0]))
                warning('This NDK lookup is intended for debug only, if you '
                        'use python-for-android much you should probably '
                        'maintain your own NDK download.')
                ndk_dir = possible_dirs[0]
        if ndk_dir is None:
            warning('Android NDK dir was not specified, exiting.')
            exit(1)
        self.ndk_dir = realpath(ndk_dir)

        # Find the NDK version, and check it against what the NDK dir
        # seems to report
        ndk_ver = None
        if user_ndk_ver:
            ndk_ver = user_ndk_ver
            if ndk_dir is not None:
                info('Got NDK version from from user argument')
        if ndk_ver is None:
            ndk_ver = environ.get('ANDROIDNDKVER', None)
            if ndk_dir is not None:
                info('Got NDK version from $ANDROIDNDKVER')

        try:
            with open(join(ndk_dir, 'RELEASE.TXT')) as fileh:
                reported_ndk_ver = fileh.read().split(' ')[0]
        except IOError:
            pass
        else:
            if ndk_ver is None:
                ndk_ver = reported_ndk_ver
                info(('Got Android NDK version from the NDK dir: '
                      'it is {}').format(ndk_ver))
            else:
                if ndk_ver != reported_ndk_ver:
                    warning('NDK version was set as {}, but checking '
                            'the NDK dir claims it is {}.'.format(
                                ndk_ver, reported_ndk_ver))
                    warning('The build will try to continue, but it may '
                            'fail and you should check '
                            'that your setting is correct.')
                    warning('If the NDK dir result is correct, you don\'t '
                            'need to manually set the NDK ver.')
        if ndk_ver is None:
            warning('Android NDK version could not be found, exiting.')
        self.ndk_ver = ndk_ver

        virtualenv = None
        if virtualenv is None:
            virtualenv = sh.which('virtualenv2')
        if virtualenv is None:
            virtualenv = sh.which('virtualenv-2.7')
        if virtualenv is None:
            virtualenv = sh.which('virtualenv')
        if virtualenv is None:
            raise IOError('Couldn\'t find a virtualenv executable, '
                          'you must install this to use p4a.')
        self.virtualenv = virtualenv
        info('Found virtualenv at {}'.format(virtualenv))

        # path to some tools
        self.ccache = sh.which("ccache")
        if not self.ccache:
            info('ccache is missing, the build will not be optimized in the '
                 'future.')
        for cython_fn in ("cython2", "cython-2.7", "cython"):
            cython = sh.which(cython_fn)
            if cython:
                self.cython = cython
                break
        if not self.cython:
            ok = False
            warning("Missing requirement: cython is not installed")

        # AND: need to change if supporting multiple archs at once
        arch = self.archs[0]
        platform_dir = arch.platform_dir
        toolchain_prefix = arch.toolchain_prefix
        command_prefix = arch.command_prefix
        self.ndk_platform = join(
            self.ndk_dir,
            'platforms',
            'android-{}'.format(self.android_api),
            platform_dir)
        if not exists(self.ndk_platform):
            warning('ndk_platform doesn\'t exist: {}'.format(self.ndk_platform))
            ok = False

        py_platform = sys.platform
        if py_platform in ['linux2', 'linux3']:
            py_platform = 'linux'

        toolchain_versions = []
        toolchain_path = join(self.ndk_dir, 'toolchains')
        if os.path.isdir(toolchain_path):
            toolchain_contents = glob.glob('{}/{}-*'.format(toolchain_path,
                                                            toolchain_prefix))
            toolchain_versions = [split(path)[-1][len(toolchain_prefix) + 1:]
                                  for path in toolchain_contents]
        else:
            warning('Could not find toolchain subdirectory!')
            ok = False
        toolchain_versions.sort()

        toolchain_versions_gcc = []
        for toolchain_version in toolchain_versions:
            if toolchain_version[0].isdigit():
                # GCC toolchains begin with a number
                toolchain_versions_gcc.append(toolchain_version)

        if toolchain_versions:
            info('Found the following toolchain versions: {}'.format(
                toolchain_versions))
            info('Picking the latest gcc toolchain, here {}'.format(
                toolchain_versions_gcc[-1]))
            toolchain_version = toolchain_versions_gcc[-1]
        else:
            warning('Could not find any toolchain for {}!'.format(
                toolchain_prefix))
            ok = False

        self.toolchain_prefix = toolchain_prefix
        self.toolchain_version = toolchain_version
        # Modify the path so that sh finds modules appropriately
        environ['PATH'] = (
            '{ndk_dir}/toolchains/{toolchain_prefix}-{toolchain_version}/'
            'prebuilt/{py_platform}-x86/bin/:{ndk_dir}/toolchains/'
            '{toolchain_prefix}-{toolchain_version}/prebuilt/'
            '{py_platform}-x86_64/bin/:{ndk_dir}:{sdk_dir}/'
            'tools:{path}').format(
                sdk_dir=self.sdk_dir, ndk_dir=self.ndk_dir,
                toolchain_prefix=toolchain_prefix,
                toolchain_version=toolchain_version,
                py_platform=py_platform, path=environ.get('PATH'))

        for executable in ("pkg-config", "autoconf", "automake", "libtoolize",
                           "tar", "bzip2", "unzip", "make", "gcc", "g++"):
            if not sh.which(executable):
                warning("Missing executable: {} is not installed".format(
                    executable))

        if not ok:
            error('{}python-for-android cannot continue; aborting{}'.format(Err_Fore.RED, Err_Fore.RESET))
            sys.exit(1)

    def __init__(self):
        super(Context, self).__init__()
        self.include_dirs = []

        self._build_env_prepared = False

        self._sdk_dir = None
        self._ndk_dir = None
        self._android_api = None
        self._ndk_ver = None

        self.toolchain_prefix = None
        self.toolchain_version = None

        # root of the toolchain
        self.setup_dirs()

        # this list should contain all Archs, it is pruned later
        self.archs = (
            ArchARM(self),
            ArchARMv7_a(self),
            Archx86(self)
            )

        ensure_dir(join(self.build_dir, 'bootstrap_builds'))
        ensure_dir(join(self.build_dir, 'other_builds'))
        # other_builds: where everything else is built

        # remove the most obvious flags that can break the compilation
        self.env.pop("LDFLAGS", None)
        self.env.pop("ARCHFLAGS", None)
        self.env.pop("CFLAGS", None)

        # set the state
        self.state = JsonStore(join(self.dist_dir, "state.db"))

    def set_archs(self, arch_names):
        all_archs = self.archs
        new_archs = set()
        for name in arch_names:
            matching = [arch for arch in all_archs if arch.arch == name]
            for match in matching:
                new_archs.add(match)
        self.archs = list(new_archs)
        if not self.archs:
            warning('Asked to compile for no Archs, so failing.')
            exit(1)
        info('Will compile for the following archs: {}'.format(
            ', '.join([arch.arch for arch in self.archs])))

    def prepare_bootstrap(self, bs):
        bs.ctx = self
        self.bootstrap = bs
        self.bootstrap.prepare_build_dir()
        self.bootstrap_build_dir = self.bootstrap.build_dir

    def prepare_dist(self, name):
        self.dist_name = name
        self.bootstrap.prepare_dist_dir(self.dist_name)

    def get_site_packages_dir(self, arch=None):
        '''Returns the location of site-packages in the python-install build
        dir.
        '''

        # AND: This *must* be replaced with something more general in
        # order to support multiple python versions and/or multiple
        # archs.
        return join(self.get_python_install_dir(),
                    'lib', 'python2.7', 'site-packages')

    def get_libs_dir(self, arch):
        '''The libs dir for a given arch.'''
        ensure_dir(join(self.libs_dir, arch))
        return join(self.libs_dir, arch)


class Distribution(object):
    '''State container for information about a distribution (i.e. an
    Android project).

    This is separate from a Bootstrap because the Bootstrap is
    concerned with building and populating the dist directory, whereas
    the dist itself could also come from e.g. a binary download.
    '''
    ctx = None

    name = None  # A name identifying the dist. May not be None.
    needs_build = False  # Whether the dist needs compiling
    url = None
    dist_dir = None  # Where the dist dir ultimately is. Should not be None.

    archs = []
    '''The arch targets that the dist is built for.'''

    recipes = []

    description = ''  # A long description

    def __init__(self, ctx):
        self.ctx = ctx

    def __str__(self):
        return '<Distribution: name {} with recipes ({})>'.format(
            # self.name, ', '.join([recipe.name for recipe in self.recipes]))
            self.name, ', '.join(self.recipes))

    def __repr__(self):
        return str(self)

    @classmethod
    def get_distribution(cls, ctx, name=None, recipes=[], allow_download=True,
                         force_build=False,
                         allow_build=True, extra_dist_dirs=[],
                         require_perfect_match=False):
        '''Takes information about the distribution, and decides what kind of
        distribution it will be.

        If parameters conflict (e.g. a dist with that name already
        exists, but doesn't have the right set of recipes),
        an error is thrown.

        Parameters
        ----------
        name : str
            The name of the distribution. If a dist with this name already '
            exists, it will be used.
        recipes : list
            The recipes that the distribution must contain.
        allow_download : bool
            Whether binary dists may be downloaded.
        allow_build : bool
            Whether the distribution may be built from scratch if necessary.
            This is always False on e.g. Windows.
        force_download: bool
            If True, only downloaded dists are considered.
        force_build : bool
            If True, the dist is forced to be built locally.
        extra_dist_dirs : list
            Any extra directories in which to search for dists.
        require_perfect_match : bool
            If True, will only match distributions with precisely the
            correct set of recipes.
        '''

        # AND: This whole function is a bit hacky, it needs checking
        # properly to make sure it follows logically correct
        # possibilities

        existing_dists = Distribution.get_distributions(ctx)

        needs_build = True  # whether the dist needs building, will be returned

        possible_dists = existing_dists

        # 0) Check if a dist with that name already exists
        if name is not None and name:
            possible_dists = [d for d in possible_dists if d.name == name]

        # 1) Check if any existing dists meet the requirements
        _possible_dists = []
        for dist in possible_dists:
            for recipe in recipes:
                if recipe not in dist.recipes:
                    break
            else:
                _possible_dists.append(dist)
        possible_dists = _possible_dists

        if possible_dists:
            info('Of the existing distributions, the following meet '
                 'the given requirements:')
            pretty_log_dists(possible_dists)
        else:
            info('No existing dists meet the given requirements!')

        # If any dist has perfect recipes, return it
        for dist in possible_dists:
            if force_build:
                continue
            if (set(dist.recipes) == set(recipes) or
                (set(recipes).issubset(set(dist.recipes)) and
                 not require_perfect_match)):
                info_notify('{} has compatible recipes, using this one'
                            .format(dist.name))
                return dist

        assert len(possible_dists) < 2

        if not name and possible_dists:
            info('Asked for dist with name {} with recipes ({}), but a dist '
                 'with this name already exists and has incompatible recipes '
                 '({})'.format(name, ', '.join(recipes),
                               ', '.join(possible_dists[0].recipes)))
            info('No compatible dist found, so exiting.')
            exit(1)

        # # 2) Check if any downloadable dists meet the requirements

        # online_dists = [('testsdl2', ['hostpython2', 'sdl2_image',
        #                               'sdl2_mixer', 'sdl2_ttf',
        #                               'python2', 'sdl2',
        #                               'pyjniussdl2', 'kivysdl2'],
        #                  'https://github.com/inclement/sdl2-example-dist/archive/master.zip'),
        #                  ]
        # _possible_dists = []
        # for dist_name, dist_recipes, dist_url in online_dists:
        #     for recipe in recipes:
        #         if recipe not in dist_recipes:
        #             break
        #     else:
        #         dist = Distribution(ctx)
        #         dist.name = dist_name
        #         dist.url = dist_url
        #         _possible_dists.append(dist)
        # # if _possible_dists

        # If we got this far, we need to build a new dist
        dist = Distribution(ctx)
        dist.needs_build = True

        if not name:
            filen = 'unnamed_dist_{}'
            i = 1
            while exists(join(ctx.dist_dir, filen.format(i))):
                i += 1
            name = filen.format(i)

        dist.name = name
        dist.dist_dir = join(ctx.dist_dir, dist.name)
        dist.recipes = recipes

        return dist

    @classmethod
    def get_distributions(cls, ctx, extra_dist_dirs=[]):
        '''Returns all the distributions found locally.'''
        if extra_dist_dirs:
            warning('extra_dist_dirs argument to get_distributions '
                    'is not yet implemented')
            exit(1)
        dist_dir = ctx.dist_dir
        folders = glob.glob(join(dist_dir, '*'))
        for dir in extra_dist_dirs:
            folders.extend(glob.glob(join(dir, '*')))

        dists = []
        for folder in folders:
            if exists(join(folder, 'dist_info.json')):
                with open(join(folder, 'dist_info.json')) as fileh:
                    dist_info = json.load(fileh)
                dist = cls(ctx)
                dist.name = folder.split('/')[-1]
                dist.dist_dir = folder
                dist.needs_build = False
                dist.recipes = dist_info['recipes']
                if 'archs' in dist_info:
                    dist.archs = dist_info['archs']
                dists.append(dist)
        return dists

    def save_info(self):
        '''
        Save information about the distribution in its dist_dir.
        '''
        with current_directory(self.dist_dir):
            info('Saving distribution info')
            with open('dist_info.json', 'w') as fileh:
                json.dump({'dist_name': self.name,
                           'archs': [arch.arch for arch in self.ctx.archs],
                           'recipes': self.ctx.recipe_build_order},
                          fileh)

    def load_info(self):
        '''Load information about the dist from the info file that p4a
        automatically creates.'''
        with current_directory(self.dist_dir):
            filen = 'dist_info.json'
            if not exists(filen):
                return None
            with open('dist_info.json', 'r') as fileh:
                dist_info = json.load(fileh)
        return dist_info


class Bootstrap(object):
    '''An Android project template, containing recipe stuff for
    compilation and templated fields for APK info.
    '''
    name = ''
    jni_subdir = '/jni'
    ctx = None

    bootstrap_dir = None

    build_dir = None
    dist_dir = None
    dist_name = None
    distribution = None

    recipe_depends = []

    can_be_chosen_automatically = True
    '''Determines whether the bootstrap can be chosen as one that
    satisfies user requirements. If False, it will not be returned
    from Bootstrap.get_bootstrap_from_recipes.
    '''

    # Other things a Bootstrap might need to track (maybe separately):
    # ndk_main.c
    # whitelist.txt
    # blacklist.txt

    @property
    def dist_dir(self):
        '''The dist dir at which to place the finished distribution.'''
        if self.distribution is None:
            warning('Tried to access {}.dist_dir, but {}.distribution '
                    'is None'.format(self, self))
            exit(1)
        return self.distribution.dist_dir

    @property
    def jni_dir(self):
        return self.name + self.jni_subdir

    def get_build_dir(self):
        return join(self.ctx.build_dir, 'bootstrap_builds', self.name)

    def get_dist_dir(self, name):
        return join(self.ctx.dist_dir, name)

    @property
    def name(self):
        modname = self.__class__.__module__
        return modname.split(".", 2)[-1]

    def prepare_build_dir(self):
        '''Ensure that a build dir exists for the recipe. This same single
        dir will be used for building all different archs.'''
        self.build_dir = self.get_build_dir()
        shprint(sh.cp, '-r',
                join(self.bootstrap_dir, 'build'),
                # join(self.ctx.root_dir,
                #      'bootstrap_templates',
                #      self.name),
                self.build_dir)
        with current_directory(self.build_dir):
            with open('project.properties', 'w') as fileh:
                fileh.write('target=android-{}'.format(self.ctx.android_api))

    def prepare_dist_dir(self, name):
        # self.dist_dir = self.get_dist_dir(name)
        ensure_dir(self.dist_dir)

    def run_distribute(self):
        # print('Default bootstrap being used doesn\'t know how '
        #       'to distribute...failing.')
        # exit(1)
        with current_directory(self.dist_dir):
            info('Saving distribution info')
            with open('dist_info.json', 'w') as fileh:
                json.dump({'dist_name': self.ctx.dist_name,
                           'bootstrap': self.ctx.bootstrap.name,
                           'archs': [arch.arch for arch in self.ctx.archs],
                           'recipes': self.ctx.recipe_build_order},
                          fileh)

    @classmethod
    def list_bootstraps(cls):
        '''Find all the available bootstraps and return them.'''
        forbidden_dirs = ('__pycache__', )
        bootstraps_dir = join(dirname(__file__), 'bootstraps')
        for name in listdir(bootstraps_dir):
            if name in forbidden_dirs:
                continue
            filen = join(bootstraps_dir, name)
            if isdir(filen):
                yield name

    @classmethod
    def get_bootstrap_from_recipes(cls, recipes, ctx):
        '''Returns a bootstrap whose recipe requirements do not conflict with
        the given recipes.'''
        info('Trying to find a bootstrap that matches the given recipes.')
        bootstraps = [cls.get_bootstrap(name, ctx)
                      for name in cls.list_bootstraps()]
        acceptable_bootstraps = []
        for bs in bootstraps:
            ok = True
            if not bs.can_be_chosen_automatically:
                ok = False
            for recipe in bs.recipe_depends:
                recipe = Recipe.get_recipe(recipe, ctx)
                if any([conflict in recipes for conflict in recipe.conflicts]):
                    ok = False
                    break
            for recipe in recipes:
                recipe = Recipe.get_recipe(recipe, ctx)
                if any([conflict in bs.recipe_depends
                        for conflict in recipe.conflicts]):
                    ok = False
                    break
            if ok:
                acceptable_bootstraps.append(bs)
        info('Found {} acceptable bootstraps: {}'.format(
            len(acceptable_bootstraps),
            [bs.name for bs in acceptable_bootstraps]))
        if acceptable_bootstraps:
            info('Using the first of these: {}'
                 .format(acceptable_bootstraps[0].name))
            return acceptable_bootstraps[0]
        return None

    @classmethod
    def get_bootstrap(cls, name, ctx):
        '''Returns an instance of a bootstrap with the given name.

        This is the only way you should access a bootstrap class, as
        it sets the bootstrap directory correctly.
        '''
        # AND: This method will need to check user dirs, and access
        # bootstraps in a slightly different way
        if name is None:
            return None
        if not hasattr(cls, 'bootstraps'):
            cls.bootstraps = {}
        if name in cls.bootstraps:
            return cls.bootstraps[name]
        mod = importlib.import_module('pythonforandroid.bootstraps.{}'
                                      .format(name))
        if len(logger.handlers) > 1:
            logger.removeHandler(logger.handlers[1])
        bootstrap = mod.bootstrap
        bootstrap.bootstrap_dir = join(ctx.root_dir, 'bootstraps', name)
        bootstrap.ctx = ctx
        return bootstrap

    def distribute_libs(self, arch, src_dirs, wildcard='*'):
        '''Copy existing arch libs from build dirs to current dist dir.'''
        info('Copying libs')
        tgt_dir = join('libs', arch.arch)
        ensure_dir(tgt_dir)
        for src_dir in src_dirs:
            for lib in glob.glob(join(src_dir, wildcard)):
                shprint(sh.cp, '-a', lib, tgt_dir)

    def distribute_javaclasses(self, javaclass_dir):
        '''Copy existing javaclasses from build dir to current dist dir.'''
        info('Copying java files')
        for filename in glob.glob(javaclass_dir):
            shprint(sh.cp, '-a', filename, 'src')

    def distribute_aars(self, arch):
        '''Process existing .aar bundles and copy to current dist dir.'''
        info('Unpacking aars')
        for aar in glob.glob(join(self.ctx.aars_dir, '*.aar')):
            self._unpack_aar(aar, arch)

    def _unpack_aar(self, aar, arch):
        '''Unpack content of .aar bundle and copy to current dist dir.'''
        with temp_directory() as temp_dir:
            name = splitext(basename(aar))[0]
            jar_name = name + '.jar'
            info("unpack {} aar".format(name))
            debug("  from {}".format(aar))
            debug("  to {}".format(temp_dir))
            shprint(sh.unzip, '-o', aar, '-d', temp_dir)

            jar_src = join(temp_dir, 'classes.jar')
            jar_tgt = join('libs', jar_name)
            debug("copy {} jar".format(name))
            debug("  from {}".format(jar_src))
            debug("  to {}".format(jar_tgt))
            ensure_dir('libs')
            shprint(sh.cp, '-a', jar_src, jar_tgt)

            so_src_dir = join(temp_dir, 'jni', arch.arch)
            so_tgt_dir = join('libs', arch.arch)
            debug("copy {} .so".format(name))
            debug("  from {}".format(so_src_dir))
            debug("  to {}".format(so_tgt_dir))
            ensure_dir(so_tgt_dir)
            so_files = glob.glob(join(so_src_dir, '*.so'))
            for f in so_files:
                shprint(sh.cp, '-a', f, so_tgt_dir)

    def strip_libraries(self, arch):
        info('Stripping libraries')
        env = arch.get_env()
        strip = which('arm-linux-androideabi-strip', env['PATH'])
        if strip is None:
            warning('Can\'t find strip in PATH...')
            return
        strip = sh.Command(strip)
        filens = shprint(sh.find, join(self.dist_dir, 'private'),
                         join(self.dist_dir, 'libs'),
                         '-iname', '*.so', _env=env).stdout.decode('utf-8')
        logger.info('Stripping libraries in private dir')
        for filen in filens.split('\n'):
            try:
                strip(filen, _env=env)
            except sh.ErrorReturnCode_1:
                logger.debug('Failed to strip ' + filen)


def build_recipes(build_order, python_modules, ctx):
    # Put recipes in correct build order
    bs = ctx.bootstrap
    info_notify("Recipe build order is {}".format(build_order))
    if python_modules:
        info_notify(
            ('The requirements ({}) were not found as recipes, they will be '
             'installed with pip.').format(', '.join(python_modules)))
    ctx.recipe_build_order = build_order

    recipes = [Recipe.get_recipe(name, ctx) for name in build_order]

    # download is arch independent
    info_main('# Downloading recipes ')
    for recipe in recipes:
        recipe.download_if_necessary()

    for arch in ctx.archs:
        info_main('# Building all recipes for arch {}'.format(arch.arch))

        info_main('# Unpacking recipes')
        for recipe in recipes:
            ensure_dir(recipe.get_build_container_dir(arch.arch))
            recipe.prepare_build_dir(arch.arch)

        info_main('# Prebuilding recipes')
        # 2) prebuild packages
        for recipe in recipes:
            info_main('Prebuilding {} for {}'.format(recipe.name, arch.arch))
            recipe.prebuild_arch(arch)
            recipe.apply_patches(arch)

        # 3) build packages
        info_main('# Building recipes')
        for recipe in recipes:
            info_main('Building {} for {}'.format(recipe.name, arch.arch))
            if recipe.should_build(arch):
                recipe.build_arch(arch)
            else:
                info('{} said it is already built, skipping'
                     .format(recipe.name))

        # 4) biglink everything
        # AND: Should make this optional 
        info_main('# Biglinking object files')
        biglink(ctx, arch)

        # 5) postbuild packages
        info_main('# Postbuilding recipes')
        for recipe in recipes:
            info_main('Postbuilding {} for {}'.format(recipe.name, arch.arch))
            recipe.postbuild_arch(arch)

    info_main('# Installing pure Python modules')
    run_pymodules_install(ctx, python_modules)

    return


def run_pymodules_install(ctx, modules):
    if not modules:
        info('There are no Python modules to install, skipping')
        return
    info('The requirements ({}) don\'t have recipes, attempting to install '
         'them with pip'.format(', '.join(modules)))
    info('If this fails, it may mean that the module has compiled '
         'components and needs a recipe.')

    venv = sh.Command(ctx.virtualenv)
    with current_directory(join(ctx.build_dir)):
        shprint(venv, '--python=python2.7', 'venv')

        info('Creating a requirements.txt file for the Python modules')
        with open('requirements.txt', 'w') as fileh:
            for module in modules:
                fileh.write('{}\n'.format(module))

        info('Installing Python modules with pip')
        info('If this fails with a message about /bin/false, this '
             'probably means the package cannot be installed with '
             'pip as it needs a compilation recipe.')

        # This bash method is what old-p4a used
        # It works but should be replaced with something better
        shprint(sh.bash, '-c', (
            "source venv/bin/activate && env CC=/bin/false CXX=/bin/false"
            "PYTHONPATH= pip install --target '{}' -r requirements.txt"
        ).format(ctx.get_site_packages_dir()))


def biglink(ctx, arch):
    # First, collate object files from each recipe
    info('Collating object files from each recipe')
    obj_dir = join(ctx.bootstrap.build_dir, 'collated_objects')
    ensure_dir(obj_dir)
    recipes = [Recipe.get_recipe(name, ctx) for name in ctx.recipe_build_order]
    for recipe in recipes:
        recipe_obj_dir = join(recipe.get_build_container_dir(arch.arch),
                              'objects_{}'.format(recipe.name))
        if not exists(recipe_obj_dir):
            info('{} recipe has no biglinkable files dir, skipping'
                 .format(recipe.name))
            continue
        files = glob.glob(join(recipe_obj_dir, '*'))
        if not len(files):
            info('{} recipe has no biglinkable files, skipping'
                 .format(recipe.name))
        info('{} recipe has object files, copying'.format(recipe.name))
        files.append(obj_dir)
        shprint(sh.cp, '-r', *files)

    env = arch.get_env()
    env['LDFLAGS'] = env['LDFLAGS'] + ' -L{}'.format(
        join(ctx.bootstrap.build_dir, 'obj', 'local', arch.arch))

    if not len(glob.glob(join(obj_dir, '*'))):
        info('There seem to be no libraries to biglink, skipping.')
        return
    info('Biglinking')
    info('target {}'.format(join(ctx.get_libs_dir(arch.arch),
                                 'libpymodules.so')))
    biglink_function(
        join(ctx.get_libs_dir(arch.arch), 'libpymodules.so'),
        obj_dir.split(' '),
        extra_link_dirs=[join(ctx.bootstrap.build_dir,
                              'obj', 'local', arch.arch)],
        env=env)


def biglink_function(soname, objs_paths, extra_link_dirs=[], env=None):
    print('objs_paths are', objs_paths)
    sofiles = []

    for directory in objs_paths:
        for fn in os.listdir(directory):
            fn = os.path.join(directory, fn)

            if not fn.endswith(".so.o"):
                continue
            if not os.path.exists(fn[:-2] + ".libs"):
                continue

            sofiles.append(fn[:-2])

    # The raw argument list.
    args = []

    for fn in sofiles:
        afn = fn + ".o"
        libsfn = fn + ".libs"

        args.append(afn)
        with open(libsfn) as fd:
            data = fd.read()
            args.extend(data.split(" "))

    unique_args = []
    while args:
        a = args.pop()
        if a in ('-L', ):
            continue
        if a not in unique_args:
            unique_args.insert(0, a)

    for dir in extra_link_dirs:
        link = '-L{}'.format(dir)
        if link not in unique_args:
            unique_args.append(link)

    cc_name = env['CC']
    cc = sh.Command(cc_name.split()[0])
    cc = cc.bake(*cc_name.split()[1:])

    shprint(cc, '-shared', '-O3', '-o', soname, *unique_args, _env=env)


def ensure_dir(filename):
    if not exists(filename):
        makedirs(filename)


def dist_from_args(ctx, dist_args):
    '''Parses out any distribution-related arguments, and uses them to
    obtain a Distribution class instance for the build.
    '''
    return Distribution.get_distribution(
        ctx,
        name=dist_args.dist_name,
        recipes=split_argument_list(dist_args.requirements),
        allow_download=dist_args.allow_download,
        allow_build=dist_args.allow_build,
        extra_dist_dirs=split_argument_list(dist_args.extra_dist_dirs),
        require_perfect_match=dist_args.require_perfect_match)


def build_dist_from_args(ctx, dist, args_list):
    '''Parses out any bootstrap related arguments, and uses them to build
    a dist.'''
    parser = argparse.ArgumentParser(
        description='Create a newAndroid project')
    parser.add_argument(
        '--bootstrap',
        help=('The name of the bootstrap type, \'pygame\' '
              'or \'sdl2\', or leave empty to let a '
              'bootstrap be chosen automatically from your '
              'requirements.'),
        default=None)
    args, unknown = parser.parse_known_args(args_list)

    bs = Bootstrap.get_bootstrap(args.bootstrap, ctx)
    build_order, python_modules, bs \
        = get_recipe_order_and_bootstrap(ctx, dist.recipes, bs)

    info('The selected bootstrap is {}'.format(bs.name))
    info_main('# Creating dist with {} bootstrap'.format(bs.name))
    bs.distribution = dist
    info_notify('Dist will have name {} and recipes ({})'.format(
        dist.name, ', '.join(dist.recipes)))

    ctx.dist_name = bs.distribution.name
    ctx.prepare_bootstrap(bs)
    ctx.prepare_dist(ctx.dist_name)

    build_recipes(build_order, python_modules, ctx)

    ctx.bootstrap.run_distribute()

    info_main('# Your distribution was created successfully, exiting.')
    info('Dist can be found at (for now) {}'
         .format(join(ctx.dist_dir, ctx.dist_name)))

    return unknown


def get_recipe_order_and_bootstrap(ctx, names, bs=None):
    '''Takes a list of recipe names and (optionally) a bootstrap. Then
    works out the dependency graph (including bootstrap recipes if
    necessary). Finally, if no bootstrap was initially selected,
    chooses one that supports all the recipes.
    '''
    graph = Graph()
    recipes_to_load = set(names)
    if bs is not None and bs.recipe_depends:
        info_notify('Bootstrap requires recipes {}'.format(bs.recipe_depends))
        recipes_to_load = recipes_to_load.union(set(bs.recipe_depends))
    recipes_to_load = list(recipes_to_load)
    recipe_loaded = []
    python_modules = []
    while recipes_to_load:
        name = recipes_to_load.pop(0)
        if name in recipe_loaded or isinstance(name, (list, tuple)):
            continue
        try:
            recipe = Recipe.get_recipe(name, ctx)
        except IOError:
            info('No recipe named {}; will attempt to install with pip'
                 .format(name))
            python_modules.append(name)
            continue
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            warning('Failed to import recipe named {}; the recipe exists '
                    'but appears broken.'.format(name))
            warning('Exception was:')
            raise
        graph.add(name, name)
        info('Loaded recipe {} (depends on {}{})'.format(
            name, recipe.depends,
            ', conflicts {}'.format(recipe.conflicts) if recipe.conflicts
            else ''))
        for depend in recipe.depends:
            graph.add(name, depend)
            recipes_to_load += recipe.depends
        for conflict in recipe.conflicts:
            if graph.conflicts(conflict):
                warning(
                    ('{} conflicts with {}, but both have been '
                     'included or pulled into the requirements.'
                     .format(recipe.name, conflict)))
                warning(
                    'Due to this conflict the build cannot continue, exiting.')
                exit(1)
        recipe_loaded.append(name)
    graph.remove_remaining_conflicts(ctx)
    if len(graph.graphs) > 1:
        info('Found multiple valid recipe sets:')
        for g in graph.graphs:
            info('    {}'.format(g.keys()))
        info_notify('Using the first of these: {}'
                    .format(graph.graphs[0].keys()))
    elif len(graph.graphs) == 0:
        warning('Didn\'t find any valid dependency graphs, exiting.')
        exit(1)
    else:
        info('Found a single valid recipe set (this is good)')

    build_order = list(graph.find_order(0))
    if bs is None:  # It would be better to check against possible
                    # orders other than the first one, but in practice
                    # there will rarely be clashes, and the user can
                    # specify more parameters if necessary to resolve
                    # them.
        bs = Bootstrap.get_bootstrap_from_recipes(build_order, ctx)
        if bs is None:
            info('Could not find a bootstrap compatible with the '
                 'required recipes.')
            info('If you think such a combination should exist, try '
                 'specifying the bootstrap manually with --bootstrap.')
            exit(1)
        info('{} bootstrap appears compatible with the required recipes.'
             .format(bs.name))
        info('Checking this...')
        recipes_to_load = bs.recipe_depends
        # This code repeats the code from earlier! Should move to a function:
        while recipes_to_load:
            name = recipes_to_load.pop(0)
            if name in recipe_loaded or isinstance(name, (list, tuple)):
                continue
            try:
                recipe = Recipe.get_recipe(name, ctx)
            except ImportError:
                info('No recipe named {}; will attempt to install with pip'
                     .format(name))
                python_modules.append(name)
                continue
            graph.add(name, name)
            info('Loaded recipe {} (depends on {}{})'.format(
                name, recipe.depends,
                ', conflicts {}'.format(recipe.conflicts) if recipe.conflicts
                else ''))
            for depend in recipe.depends:
                graph.add(name, depend)
                recipes_to_load += recipe.depends
            for conflict in recipe.conflicts:
                if graph.conflicts(conflict):
                    warning(
                        ('{} conflicts with {}, but both have been '
                         'included or pulled into the requirements.'
                         .format(recipe.name, conflict)))
                    warning('Due to this conflict the build cannot continue, '
                            'exiting.')
                    exit(1)
            recipe_loaded.append(name)
        graph.remove_remaining_conflicts(ctx)
        build_order = list(graph.find_order(0))
    return build_order, python_modules, bs

    # Do a final check that the new bs doesn't pull in any conflicts


def split_argument_list(l):
    if not len(l):
        return []
    return re.split(r'[ ,]*', l)


class ToolchainCL(object):

    def __init__(self):
        self._ctx = None

        parser = argparse.ArgumentParser(
                description="Tool for managing the Android / Python toolchain",
                usage="""toolchain <command> [<args>]

Available commands:
adb           Runs adb binary from the detected SDK dir
apk           Create an APK using the given distribution
bootstraps    List all the bootstraps available to build with.
build_status  Informations about the current build
create        Build an android project with all recipes
clean_all     Delete all build components
clean_builds  Delete all build caches
clean_dists   Delete all compiled distributions
clean_download_cache Delete any downloaded recipe packages
clean_recipe_build   Delete the build files of a recipe
distributions List all distributions
export_dist   Copies a created dist to an output directory
logcat        Runs logcat from the detected SDK dir
print_context_info   Prints debug informations
recipes       List all the available recipes
sdk_tools     Runs android binary from the detected SDK dir
symlink_dist  Symlinks a created dist to an output directory

Planned commands:
build_dist
""")
        parser.add_argument("command", help="Command to run")

        # General options
        parser.add_argument(
            '--debug', dest='debug', action='store_true',
            help='Display debug output and all build info')
        parser.add_argument(
            '--sdk_dir', dest='sdk_dir', default='',
            help='The filepath where the Android SDK is installed')
        parser.add_argument(
            '--ndk_dir', dest='ndk_dir', default='',
            help='The filepath where the Android NDK is installed')
        parser.add_argument(
            '--android_api', dest='android_api', default=0, type=int,
            help='The Android API level to build against.')
        parser.add_argument(
            '--ndk_version', dest='ndk_version', default='',
            help=('The version of the Android NDK. This is optional, '
                  'we try to work it out automatically from the ndk_dir.'))


        # AND: This option doesn't really fit in the other categories, the
        # arg structure needs a rethink
        parser.add_argument(
            '--arch',
            help='The archs to build for, separated by commas.',
            default='armeabi')

        # Options for specifying the Distribution
        parser.add_argument(
            '--dist_name',
            help='The name of the distribution to use or create',
            default='')
        parser.add_argument(
            '--requirements',
            help=('Dependencies of your app, should be recipe names or '
                  'Python modules'),
            default='')

        add_boolean_option(
            parser, ["allow-download"],
            default=False,
            description='Whether to allow binary dist download:')

        add_boolean_option(
            parser, ["allow-build"],
            default=True,
            description='Whether to allow compilation of a new distribution:')

        add_boolean_option(
            parser, ["force-build"],
            default=False,
            description='Whether to force compilation of a new distribution:')

        parser.add_argument(
            '--extra-dist-dirs', '--extra_dist_dirs',
            dest='extra_dist_dirs', default='',
            help='Directories in which to look for distributions')

        add_boolean_option(
            parser, ["require-perfect-match"],
            default=False,
            description=('Whether the dist recipes must perfectly match '
                         'those requested'))


        self._read_configuration()

        args, unknown = parser.parse_known_args(sys.argv[1:])
        self.dist_args = args

        if args.debug:
            logger.setLevel(logging.DEBUG)
        self.sdk_dir = args.sdk_dir
        self.ndk_dir = args.ndk_dir
        self.android_api = args.android_api
        self.ndk_version = args.ndk_version

        self._archs = split_argument_list(args.arch)

        # AND: Fail nicely if the args aren't handled yet
        if args.extra_dist_dirs:
            warning('Received --extra_dist_dirs but this arg currently is not '
                    'handled, exiting.')
            exit(1)
        if args.allow_download:
            warning('Received --allow_download but this arg currently is not '
                    'handled, exiting.')
            exit(1)
        # if args.allow_build:
        #     warning('Received --allow_build but this arg currently is not '
        #             'handled, exiting.')
        #     exit(1)

        if not hasattr(self, args.command):
            print('Unrecognized command')
            parser.print_help()
            exit(1)
        getattr(self, args.command)(unknown)

    def _read_configuration(self):
        # search for a .p4a configuration file in the current directory
        if not exists(".p4a"):
            return
        info("Reading .p4a configuration")
        with open(".p4a") as fd:
            lines = fd.readlines()
        lines = [shlex.split(line)
                 for line in lines if not line.startswith("#")]
        for line in lines:
            for arg in line:
                sys.argv.append(arg)

    @property
    def ctx(self):
        if self._ctx is None:
            self._ctx = Context()
        return self._ctx

    def recipes(self, args):
        parser = argparse.ArgumentParser(
                description="List all the available recipes")
        parser.add_argument(
                "--compact", action="store_true", default=False,
                help="Produce a compact list suitable for scripting")

        add_boolean_option(
            parser, ["color"],
            default=True,
            description='Whether the output should be colored:')

        args = parser.parse_args(args)

        Fore = Out_Fore
        Style = Out_Style
        if not args.color:
            Fore = Null_Fore
            Style = Null_Style

        if args.compact:
            print(" ".join(list(Recipe.list_recipes())))
        else:
            ctx = self.ctx
            for name in sorted(Recipe.list_recipes()):
                recipe = Recipe.get_recipe(name, ctx)
                version = str(recipe.version)
                print('{Fore.BLUE}{Style.BRIGHT}{recipe.name:<12} '
                      '{Style.RESET_ALL}{Fore.LIGHTBLUE_EX}'
                      '{version:<8}{Style.RESET_ALL}'.format(
                          recipe=recipe, Fore=Fore, Style=Style,
                          version=version))
                print('    {Fore.GREEN}depends: {recipe.depends}'
                      '{Fore.RESET}'.format(recipe=recipe, Fore=Fore))
                if recipe.conflicts:
                    print('    {Fore.RED}conflicts: {recipe.conflicts}'
                          '{Fore.RESET}'
                          .format(recipe=recipe, Fore=Fore))
                if recipe.opt_depends:
                    print('    {Fore.YELLOW}optional depends: '
                          '{recipe.opt_depends}{Fore.RESET}'
                          .format(recipe=recipe, Fore=Fore))

    def bootstraps(self, args):
        '''List all the bootstraps available to build with.'''
        for bs in Bootstrap.list_bootstraps():
            bs = Bootstrap.get_bootstrap(bs, self.ctx)
            print('{Fore.BLUE}{Style.BRIGHT}{bs.name}{Style.RESET_ALL}'
                  .format(bs=bs, Fore=Out_Fore, Style=Out_Style))
            print('    {Fore.GREEN}depends: {bs.recipe_depends}{Fore.RESET}'
                  .format(bs=bs, Fore=Out_Fore))

    def clean_all(self, args):
        '''Delete all build components; the package cache, package builds,
        bootstrap builds and distributions.'''
        parser = argparse.ArgumentParser(
                description="Clean the build cache, downloads and dists")
        parsed_args = parser.parse_args(args)
        ctx = Context()
        self.clean_dists(args)
        self.clean_builds(args)
        self.clean_download_cache(args)

    def clean_dists(self, args):
        '''Delete all compiled distributions in the internal distribution
        directory.'''
        parser = argparse.ArgumentParser(
                description="Delete any distributions that have been built.")
        args = parser.parse_args(args)
        ctx = Context()
        if exists(ctx.dist_dir):
            shutil.rmtree(ctx.dist_dir)

    def clean_builds(self, args):
        '''Delete all build caches for each recipe, python-install, java code
        and compiled libs collection.

        This does *not* delete the package download cache or the final
        distributions.  You can also use clean_recipe_build to delete the build
        of a specific recipe.
        '''
        parser = argparse.ArgumentParser(
                description="Delete all build files (but not download caches)")
        args = parser.parse_args(args)
        ctx = Context()
        # if exists(ctx.dist_dir):
        #     shutil.rmtree(ctx.dist_dir)
        if exists(ctx.build_dir):
            shutil.rmtree(ctx.build_dir)
        if exists(ctx.python_installs_dir):
            shutil.rmtree(ctx.python_installs_dir)
        libs_dir = join(self.ctx.build_dir, 'libs_collections')
        if exists(libs_dir):
            shutil.rmtree(libs_dir)

    def clean_recipe_build(self, args):
        '''Deletes the build files of the given recipe.

        This is intended for debug purposes, you may experience
        strange behaviour or problems with some recipes (if their
        build has done unexpected state changes). If this happens, run
        clean_builds, or attempt to clean other recipes until things
        work again.
        '''
        parser = argparse.ArgumentParser(
            description="Delete all build files for the given recipe name.")
        parser.add_argument('recipe', help='The recipe name')
        args = parser.parse_args(args)

        recipe = Recipe.get_recipe(args.recipe, self.ctx)
        info('Cleaning build for {} recipe.'.format(recipe.name))
        recipe.clean_build()

    def clean_download_cache(self, args):
        '''
        Deletes any downloaded recipe packages.

        This does *not* delete the build caches or final distributions.
        '''
        parser = argparse.ArgumentParser(
                description="Delete all download caches")
        args = parser.parse_args(args)
        ctx = Context()
        if exists(ctx.packages_path):
            shutil.rmtree(ctx.packages_path)

    @require_prebuilt_dist
    def export_dist(self, args):
        '''Copies a created dist to an output dir.

        This makes it easy to navigate to the dist to investigate it
        or call build.py, though you do not in general need to do this
        and can use the apk command instead.
        '''
        parser = argparse.ArgumentParser(
            description='Copy a created dist to a given directory')
        parser.add_argument('--output', help=('The output dir to copy to'),
                            required=True)
        args = parser.parse_args(args)

        ctx = self.ctx
        dist = dist_from_args(ctx, self.dist_args)
        if dist.needs_build:
            info('You asked to export a dist, but there is no dist '
                 'with suitable recipes available. For now, you must '
                 ' create one first with the create argument.')
            exit(1)
        shprint(sh.cp, '-r', dist.dist_dir, args.output)

    @require_prebuilt_dist
    def symlink_dist(self, args):
        '''Symlinks a created dist to an output dir.

        This makes it easy to navigate to the dist to investigate it
        or call build.py, though you do not in general need to do this
        and can use the apk command instead.

        '''
        parser = argparse.ArgumentParser(
            description='Symlink a created dist to a given directory')
        parser.add_argument('--output', help=('The output dir to copy to'),
                            required=True)
        args = parser.parse_args(args)

        ctx = self.ctx
        dist = dist_from_args(ctx, self.dist_args)
        if dist.needs_build:
            info('You asked to symlink a dist, but there is no dist '
                 'with suitable recipes available. For now, you must '
                 'create one first with the create argument.')
            exit(1)
        shprint(sh.ln, '-s', dist.dist_dir, args.output)

    # def _get_dist(self):
    #     ctx = self.ctx
    #     dist = dist_from_args(ctx, self.dist_args)

    @property
    def _dist(self):
        ctx = self.ctx
        dist = dist_from_args(ctx, self.dist_args)
        return dist

    @require_prebuilt_dist
    def apk(self, args):
        '''Create an APK using the given distribution.'''

        # AND: Need to add a parser here for any extra options
        # parser = argparse.ArgumentParser(
        #     description='Build an APK')
        # args = parser.parse_args(args)

        ctx = self.ctx
        dist = self._dist

        # Manually fixing these arguments at the string stage is
        # unsatisfactory and should probably be changed somehow, but
        # we can't leave it until later as the build.py scripts assume
        # they are in the current directory.
        for i, arg in enumerate(args[:-1]):
            if arg in ('--dir', '--private'):
                args[i+1] = realpath(expanduser(args[i+1]))

        build = imp.load_source('build', join(dist.dist_dir, 'build.py'))
        with current_directory(dist.dist_dir):
            build.parse_args(args)
            shprint(sh.ant, 'debug', _tail=20, _critical=True)

        # AND: This is very crude, needs improving. Also only works
        # for debug for now.
        info_main('# Copying APK to current directory')
        apks = glob.glob(join(dist.dist_dir, 'bin', '*-*-debug.apk'))
        if len(apks) == 0:
            raise ValueError('Couldn\'t find the built APK')
        if len(apks) > 1:
            info('More than one built APK found...guessing you '
                 'just built {}'.format(apks[-1]))
        shprint(sh.cp, apks[-1], './')

    @require_prebuilt_dist
    def create(self, args):
        '''Create a distribution directory if it doesn't already exist, run
        any recipes if necessary, and build the apk.
        '''
        pass  # The decorator does this for us
        # ctx = self.ctx

        # dist = dist_from_args(ctx, self.dist_args)
        # if not dist.needs_build:
        #     info('You asked to create a distribution, but a dist with '
        #          'this name already exists. If you don\'t want to use '
        #          'it, you must delete it and rebuild, or create your '
        #          'new dist with a different name.')
        #     exit(1)
        # info('Ready to create dist {}, contains recipes {}'.format(
        #     dist.name, ', '.join(dist.recipes)))

        # build_dist_from_args(ctx, dist, args)

    def print_context_info(self, args):
        '''Prints some debug information about which system paths
        python-for-android will internally use for package building, along
        with information about where the Android SDK and NDK will be called
        from.'''
        ctx = Context()
        for attribute in ('root_dir', 'build_dir', 'dist_dir', 'libs_dir',
                          'ccache', 'cython', 'sdk_dir', 'ndk_dir',
                          'ndk_platform', 'ndk_ver', 'android_api'):
            print('{} is {}'.format(attribute, getattr(ctx, attribute)))

    def archs(self, args):
        '''List the target architectures available to be built for.'''
        print('{Style.BRIGHT}Available target architectures are:'
              '{Style.RESET_ALL}'.format(Style=Out_Style))
        for arch in self.ctx.archs:
            print('    {}'.format(arch.arch))

    def dists(self, args):
        '''The same as :meth:`distributions`.'''
        self.distributions(args)

    def distributions(self, args):
        '''Lists all distributions currently available (i.e. that have already
        been built).'''
        ctx = Context()
        dists = Distribution.get_distributions(ctx)

        if dists:
            print('{Style.BRIGHT}Distributions currently installed are:'
                  '{Style.RESET_ALL}'.format(Style=Out_Style, Fore=Out_Fore))
            pretty_log_dists(dists, print)
        else:
            print('{Style.BRIGHT}There are no dists currently built.'
                  '{Style.RESET_ALL}'.format(Style=Out_Style))

    def delete_dist(self, args):
        dist = self._dist
        if dist.needs_build:
            info('No dist exists that matches your specifications, '
                 'exiting without deleting.')
        shutil.rmtree(dist.dist_dir)

    def sdk_tools(self, args):
        '''Runs the android binary from the detected SDK directory, passing
        all arguments straight to it. This binary is used to install
        e.g. platform-tools for different API level targets. This is
        intended as a convenience function if android is not in your
        $PATH.
        '''
        parser = argparse.ArgumentParser(
            description='Run a binary from the /path/to/sdk/tools directory')
        parser.add_argument('tool', help=('The tool binary name to run'))
        args, unknown = parser.parse_known_args(args)

        ctx = self.ctx
        ctx.prepare_build_environment(user_sdk_dir=self.sdk_dir,
                                      user_ndk_dir=self.ndk_dir,
                                      user_android_api=self.android_api,
                                      user_ndk_ver=self.ndk_version)
        android = sh.Command(join(ctx.sdk_dir, 'tools', args.tool))
        output = android(
            *unknown, _iter=True, _out_bufsize=1, _err_to_out=True)
        for line in output:
            sys.stdout.write(line)
            sys.stdout.flush()

    def adb(self, args):
        '''Runs the adb binary from the detected SDK directory, passing all
        arguments straight to it. This is intended as a convenience
        function if adb is not in your $PATH.
        '''
        ctx = self.ctx
        ctx.prepare_build_environment(user_sdk_dir=self.sdk_dir,
                                      user_ndk_dir=self.ndk_dir,
                                      user_android_api=self.android_api,
                                      user_ndk_ver=self.ndk_version)
        if platform in ('win32', 'cygwin'):
            adb = sh.Command(join(ctx.sdk_dir, 'platform-tools', 'adb.exe'))
        else:
            adb = sh.Command(join(ctx.sdk_dir, 'platform-tools', 'adb'))
        info_notify('Starting adb...')
        output = adb(args, _iter=True, _out_bufsize=1, _err_to_out=True)
        for line in output:
            sys.stdout.write(line)
            sys.stdout.flush()

    def logcat(self, args):
        '''Runs ``adb logcat`` using the adb binary from the detected SDK
        directory. All extra args are passed as arguments to logcat.'''
        self.adb(['logcat'] + args)

    def build_status(self, args):

        print('{Style.BRIGHT}Bootstraps whose core components are probably '
              'already built:{Style.RESET_ALL}'.format(Style=Out_Style))
        for filen in os.listdir(join(self.ctx.build_dir, 'bootstrap_builds')):
            print('    {Fore.GREEN}{Style.BRIGHT}{filen}{Style.RESET_ALL}'
                  .format(filen=filen, Fore=Out_Fore, Style=Out_Style))

        print('{Style.BRIGHT}Recipes that are probably already built:'
              '{Style.RESET_ALL}'.format(Style=Out_Style))
        if exists(join(self.ctx.build_dir, 'other_builds')):
            for filen in sorted(
                    os.listdir(join(self.ctx.build_dir, 'other_builds'))):
                name = filen.split('-')[0]
                dependencies = filen.split('-')[1:]
                recipe_str = ('    {Style.BRIGHT}{Fore.GREEN}{name}'
                              '{Style.RESET_ALL}'.format(
                                  Style=Out_Style, name=name, Fore=Out_Fore))
                if dependencies:
                    recipe_str += (
                        ' ({Fore.BLUE}with ' + ', '.join(dependencies) +
                        '{Fore.RESET})').format(Fore=Out_Fore)
                recipe_str += '{Style.RESET_ALL}'.format(Style=Out_Style)
                print(recipe_str)


def main():
    ToolchainCL()

if __name__ == "__main__":
    main()
