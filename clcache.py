#!/usr/bin/env python
#
# This file is part of the clcache project.
#
# The contents of this file are subject to the BSD 3-Clause License, the
# full text of which is available in the accompanying LICENSE file at the
# root directory of this project.
#
from ctypes import windll, wintypes
import codecs
from collections import defaultdict, namedtuple
from contextlib import closing
import errno
import hashlib
import json
import os
from shutil import copyfile, rmtree
import subprocess
from subprocess import Popen, PIPE
import sys
import multiprocessing
import re

VERSION = "3.2.0-dev"

HashAlgorithm = hashlib.md5

# try to use os.scandir or scandir.scandir
# fall back to os.walk if not found
try:
    import scandir # pylint: disable=wrong-import-position
    WALK = scandir.walk
except ImportError:
    WALK = os.walk

# The codec that is used by clcache to store compiler STDOUR and STDERR in
# output.txt and stderr.txt.
# This codec is up to us and only used for clcache internal storage.
# For possible values see https://docs.python.org/2/library/codecs.html
CACHE_COMPILER_OUTPUT_STORAGE_CODEC = 'utf-8'

# The cl default codec
CL_DEFAULT_CODEC = 'mbcs'

# Manifest file will have at most this number of hash lists in it. Need to avoi
# manifests grow too large.
MAX_MANIFEST_HASHES = 100

# String, by which BASE_DIR will be replaced in paths, stored in manifests.
# ? is invalid character for file name, so it seems ok
# to use it as mark for relative path.
BASEDIR_REPLACEMENT = '?'

# `includeFiles`: list of paths to include files, which this source file uses
# `includesContentToObjectMap`: dictionary
#   key: cumulative hash of all include files' content in includeFiles
#   value: key in the cache, under which the object file is stored
Manifest = namedtuple('Manifest', ['includeFiles', 'includesContentToObjectMap'])


def printBinary(stream, rawData):
    stream.buffer.write(rawData)


def basenameWithoutExtension(path):
    basename = os.path.basename(path)
    return os.path.splitext(basename)[0]


def filesBeneath(path):
    for path, _, filenames in WALK(path):
        for filename in filenames:
            yield os.path.join(path, filename)


class ObjectCacheLockException(Exception):
    pass


class LogicException(Exception):
    def __init__(self, message):
        super(LogicException, self).__init__(message)
        self.message = message

    def __str__(self):
        return repr(self.message)


class ManifestSection(object):
    def __init__(self, manifestSectionDir):
        self.manifestSectionDir = manifestSectionDir

    def manifestPath(self, manifestHash):
        return os.path.join(self.manifestSectionDir, manifestHash + ".json")

    def setManifest(self, manifestHash, manifest):
        ensureDirectoryExists(self.manifestSectionDir)
        with open(self.manifestPath(manifestHash), 'w') as outFile:
            # Converting namedtuple to JSON via OrderedDict preserves key names and keys order
            json.dump(manifest._asdict(), outFile, indent=2)

    def getManifest(self, manifestHash):
        fileName = self.manifestPath(manifestHash)
        if not os.path.exists(fileName):
            return None
        try:
            with open(fileName, 'r') as inFile:
                doc = json.load(inFile)
                return Manifest(doc['includeFiles'], doc['includesContentToObjectMap'])
        except IOError:
            return None


class ManifestsManager(object):
    # Bump this counter whenever the current manifest file format changes.
    # E.g. changing the file format from {'oldkey': ...} to {'newkey': ...} requires
    # invalidation, such that a manifest that was stored using the old format is not
    # interpreted using the new format. Instead the old file will not be touched
    # again due to a new manifest hash and is cleaned away after some time.
    MANIFEST_FILE_FORMAT_VERSION = 3

    def __init__(self, manifestsRootDir):
        self._manifestsRootDir = manifestsRootDir

    def manifestSection(self, manifestHash):
        return ManifestSection(os.path.join(self._manifestsRootDir, manifestHash[:2]))

    def clean(self, maxManifestsSize):
        manifestFileInfos = []
        for filepath in filesBeneath(self._manifestsRootDir):
            try:
                manifestFileInfos.append((os.stat(filepath), filepath))
            except OSError:
                pass

        manifestFileInfos.sort(key=lambda t: t[0].st_atime, reverse=True)

        currentSize = 0
        for stat, filepath in manifestFileInfos:
            currentSize += stat.st_size
            if currentSize < maxManifestsSize:
                # skip as long as maximal size not reached
                continue
            os.remove(filepath)

    @staticmethod
    def getManifestHash(compilerBinary, commandLine, sourceFile):
        compilerHash = getCompilerHash(compilerBinary)

        # NOTE: We intentionally do not normalize command line to include
        # preprocessor options. In direct mode we do not perform
        # preprocessing before cache lookup, so all parameters are important
        additionalData = "{}|{}|{}".format(
            compilerHash, commandLine, ManifestsManager.MANIFEST_FILE_FORMAT_VERSION)
        return getFileHash(sourceFile, additionalData)

    @staticmethod
    def getIncludesContentHash(listOfHeaderHashes):
        return HashAlgorithm(','.join(listOfHeaderHashes).encode()).hexdigest()


class ObjectCacheLock(object):
    """ Implements a lock for the object cache which
    can be used in 'with' statements. """
    INFINITE = 0xFFFFFFFF
    WAIT_ABANDONED_CODE = 0x00000080

    def __init__(self, mutexName, timeoutMs):
        mutexName = 'Local\\' + mutexName
        self._mutex = windll.kernel32.CreateMutexW(
            wintypes.INT(0),
            wintypes.INT(0),
            mutexName)
        self._timeoutMs = timeoutMs
        self._acquired = False
        assert self._mutex

    def __enter__(self):
        if not self._acquired:
            self.acquire()

    def __exit__(self, typ, value, traceback):
        if self._acquired:
            self.release()

    def __del__(self):
        windll.kernel32.CloseHandle(self._mutex)

    def acquire(self):
        result = windll.kernel32.WaitForSingleObject(
            self._mutex, wintypes.INT(self._timeoutMs))
        if result not in [0, self.WAIT_ABANDONED_CODE]:
            errorString = 'Error! WaitForSingleObject returns {result}, last error {error}'.format(
                result=result,
                error=windll.kernel32.GetLastError())
            raise ObjectCacheLockException(errorString)
        self._acquired = True

    def release(self):
        windll.kernel32.ReleaseMutex(self._mutex)
        self._acquired = False


class ObjectCache(object):
    def __init__(self):
        try:
            self.dir = os.environ["CLCACHE_DIR"]
        except KeyError:
            self.dir = os.path.join(os.path.expanduser("~"), "clcache")

        manifestsRootDir = os.path.join(self.dir, "manifests")
        ensureDirectoryExists(manifestsRootDir)
        self.manifestsManager = ManifestsManager(manifestsRootDir)

        self.objectsDir = os.path.join(self.dir, "objects")
        ensureDirectoryExists(self.objectsDir)
        lockName = self.cacheDirectory().replace(':', '-').replace('\\', '-')
        timeoutMs = int(os.environ.get('CLCACHE_OBJECT_CACHE_TIMEOUT_MS', 10 * 1000))
        self.lock = ObjectCacheLock(lockName, timeoutMs)

    def cacheDirectory(self):
        return self.dir

    def clean(self, stats, maximumSize):
        currentSize = stats.currentCacheSize()
        if currentSize < maximumSize:
            return

        # Free at least 10% to avoid cleaning up too often which
        # is a big performance hit with large caches.
        effectiveMaximumSizeOverall = maximumSize * 0.9

        # Split limit in manifests (10 %) and objects (90 %)
        effectiveMaximumSizeManifests = effectiveMaximumSizeOverall * 0.1
        effectiveMaximumSizeObjects = effectiveMaximumSizeOverall - effectiveMaximumSizeManifests

        # Clean manifests
        self.manifestsManager.clean(effectiveMaximumSizeManifests)

        # Clean objects
        objects = [os.path.join(root, "object")
                   for root, _, files in WALK(self.objectsDir)
                   if "object" in files]

        objectInfos = []
        for o in objects:
            try:
                objectInfos.append((os.stat(o), o))
            except OSError:
                pass

        objectInfos.sort(key=lambda t: t[0].st_atime)

        # compute real current size to fix up the stored cacheSize
        currentSize = sum(x[0].st_size for x in objectInfos)

        removedItems = 0
        for stat, fn in objectInfos:
            rmtree(os.path.split(fn)[0], ignore_errors=True)
            removedItems += 1
            currentSize -= stat.st_size
            if currentSize < effectiveMaximumSizeObjects:
                break

        stats.setCacheSize(currentSize)

        stats.setNumCacheEntries(len(objectInfos) - removedItems)

    def removeObjects(self, stats, removedObjects):
        for o in removedObjects:
            dirPath = self._cacheEntryDir(o)
            if not os.path.exists(dirPath):
                continue  # May be if object already evicted.
            objectPath = os.path.join(dirPath, "object")
            if os.path.exists(objectPath):
                # May be absent if this if cached compiler
                # output (for preprocess-only).
                fileStat = os.stat(objectPath)
                stats.unregisterCacheEntry(fileStat.st_size)
            rmtree(dirPath, ignore_errors=True)

    @staticmethod
    def computeKey(compilerBinary, commandLine):
        ppcmd = [compilerBinary, "/EP"]
        ppcmd += [arg for arg in commandLine if arg not in ("-c", "/c")]
        preprocessor = Popen(ppcmd, stdout=PIPE, stderr=PIPE)
        (preprocessedSourceCode, ppStderrBinary) = preprocessor.communicate()

        if preprocessor.returncode != 0:
            printBinary(sys.stderr, ppStderrBinary)
            print("clcache: preprocessor failed", file=sys.stderr)
            sys.exit(preprocessor.returncode)

        compilerHash = getCompilerHash(compilerBinary)
        normalizedCmdLine = ObjectCache._normalizedCommandLine(commandLine)

        h = HashAlgorithm()
        h.update(compilerHash.encode("UTF-8"))
        h.update(' '.join(normalizedCmdLine).encode("UTF-8"))
        h.update(preprocessedSourceCode)
        return h.hexdigest()

    @staticmethod
    def getHash(dataString):
        hasher = HashAlgorithm()
        hasher.update(dataString.encode("UTF-8"))
        return hasher.hexdigest()

    @staticmethod
    def getDirectCacheKey(manifestHash, includesContentHash):
        # We must take into account manifestHash to avoid
        # collisions when different source files use the same
        # set of includes.
        return ObjectCache.getHash(manifestHash + includesContentHash)

    def hasEntry(self, key):
        return os.path.exists(self.cachedObjectName(key)) or os.path.exists(self._cachedCompilerOutputName(key))

    def setEntry(self, key, objectFileName, compilerOutput, compilerStderr):
        ensureDirectoryExists(self._cacheEntryDir(key))
        if objectFileName is not None:
            copyOrLink(objectFileName, self.cachedObjectName(key))
        with open(self._cachedCompilerOutputName(key), 'wb') as f:
            f.write(compilerOutput.encode(CACHE_COMPILER_OUTPUT_STORAGE_CODEC))
        if compilerStderr != '':
            with open(self._cachedCompilerStderrName(key), 'wb') as f:
                f.write(compilerStderr.encode(CACHE_COMPILER_OUTPUT_STORAGE_CODEC))

    def cachedObjectName(self, key):
        return os.path.join(self._cacheEntryDir(key), "object")

    def cachedCompilerOutput(self, key):
        with open(self._cachedCompilerOutputName(key), 'rb') as f:
            return f.read().decode(CACHE_COMPILER_OUTPUT_STORAGE_CODEC)

    def cachedCompilerStderr(self, key):
        fileName = self._cachedCompilerStderrName(key)
        if os.path.exists(fileName):
            with open(fileName, 'rb') as f:
                return f.read().decode(CACHE_COMPILER_OUTPUT_STORAGE_CODEC)
        return ''

    def _cacheEntryDir(self, key):
        return os.path.join(self.objectsDir, key[:2], key)

    def _cachedCompilerOutputName(self, key):
        return os.path.join(self._cacheEntryDir(key), "output.txt")

    def _cachedCompilerStderrName(self, key):
        return os.path.join(self._cacheEntryDir(key), "stderr.txt")

    @staticmethod
    def _normalizedCommandLine(cmdline):
        # Remove all arguments from the command line which only influence the
        # preprocessor; the preprocessor's output is already included into the
        # hash sum so we don't have to care about these switches in the
        # command line as well.
        argsToStrip = ("AI", "C", "E", "P", "FI", "u", "X",
                       "FU", "D", "EP", "Fx", "U", "I")

        # Also remove the switch for specifying the output file name; we don't
        # want two invocations which are identical except for the output file
        # name to be treated differently.
        argsToStrip += ("Fo",)

        return [arg for arg in cmdline
                if not (arg[0] in "/-" and arg[1:].startswith(argsToStrip))]


class PersistentJSONDict(object):
    def __init__(self, fileName):
        self._dirty = False
        self._dict = {}
        self._fileName = fileName
        try:
            with open(self._fileName, 'r') as f:
                self._dict = json.load(f)
        except IOError:
            pass

    def save(self):
        if self._dirty:
            with open(self._fileName, 'w') as f:
                json.dump(self._dict, f, sort_keys=True, indent=4)

    def __setitem__(self, key, value):
        self._dict[key] = value
        self._dirty = True

    def __getitem__(self, key):
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__


class Configuration(object):
    _defaultValues = {"MaximumCacheSize": 1073741824} # 1 GiB

    def __init__(self, objectCache):
        self._cfg = PersistentJSONDict(os.path.join(objectCache.cacheDirectory(),
                                                    "config.txt"))
        for setting, defaultValue in self._defaultValues.items():
            if setting not in self._cfg:
                self._cfg[setting] = defaultValue

    def maximumCacheSize(self):
        return self._cfg["MaximumCacheSize"]

    def setMaximumCacheSize(self, size):
        self._cfg["MaximumCacheSize"] = size

    def save(self):
        self._cfg.save()

    def close(self):
        self.save()


class CacheStatistics(object):
    RESETTABLE_KEYS = {
        "CallsWithInvalidArgument",
        "CallsWithoutSourceFile",
        "CallsWithMultipleSourceFiles",
        "CallsWithPch",
        "CallsForLinking",
        "CallsForExternalDebugInfo",
        "CallsForPreprocessing",
        "CacheHits",
        "CacheMisses",
        "EvictedMisses",
        "HeaderChangedMisses",
        "SourceChangedMisses",
    }
    NON_RESETTABLE_KEYS = {
        "CacheEntries",
        "CacheSize",
    }

    def __init__(self, objectCache):
        self._stats = PersistentJSONDict(os.path.join(objectCache.cacheDirectory(),
                                                      "stats.txt"))
        for k in CacheStatistics.RESETTABLE_KEYS | CacheStatistics.NON_RESETTABLE_KEYS:
            if k not in self._stats:
                self._stats[k] = 0

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def numCallsWithInvalidArgument(self):
        return self._stats["CallsWithInvalidArgument"]

    def registerCallWithInvalidArgument(self):
        self._stats["CallsWithInvalidArgument"] += 1

    def numCallsWithoutSourceFile(self):
        return self._stats["CallsWithoutSourceFile"]

    def registerCallWithoutSourceFile(self):
        self._stats["CallsWithoutSourceFile"] += 1

    def numCallsWithMultipleSourceFiles(self):
        return self._stats["CallsWithMultipleSourceFiles"]

    def registerCallWithMultipleSourceFiles(self):
        self._stats["CallsWithMultipleSourceFiles"] += 1

    def numCallsWithPch(self):
        return self._stats["CallsWithPch"]

    def registerCallWithPch(self):
        self._stats["CallsWithPch"] += 1

    def numCallsForLinking(self):
        return self._stats["CallsForLinking"]

    def registerCallForLinking(self):
        self._stats["CallsForLinking"] += 1

    def numCallsForExternalDebugInfo(self):
        return self._stats["CallsForExternalDebugInfo"]

    def registerCallForExternalDebugInfo(self):
        self._stats["CallsForExternalDebugInfo"] += 1

    def numEvictedMisses(self):
        return self._stats["EvictedMisses"]

    def registerEvictedMiss(self):
        self.registerCacheMiss()
        self._stats["EvictedMisses"] += 1

    def numHeaderChangedMisses(self):
        return self._stats["HeaderChangedMisses"]

    def registerHeaderChangedMiss(self):
        self.registerCacheMiss()
        self._stats["HeaderChangedMisses"] += 1

    def numSourceChangedMisses(self):
        return self._stats["SourceChangedMisses"]

    def registerSourceChangedMiss(self):
        self.registerCacheMiss()
        self._stats["SourceChangedMisses"] += 1

    def numCacheEntries(self):
        return self._stats["CacheEntries"]

    def setNumCacheEntries(self, number):
        self._stats["CacheEntries"] = number

    def registerCacheEntry(self, size):
        self._stats["CacheEntries"] += 1
        self._stats["CacheSize"] += size

    def unregisterCacheEntry(self, size):
        self._stats["CacheEntries"] -= 1
        self._stats["CacheSize"] -= size

    def currentCacheSize(self):
        return self._stats["CacheSize"]

    def setCacheSize(self, size):
        self._stats["CacheSize"] = size

    def numCacheHits(self):
        return self._stats["CacheHits"]

    def registerCacheHit(self):
        self._stats["CacheHits"] += 1

    def numCacheMisses(self):
        return self._stats["CacheMisses"]

    def registerCacheMiss(self):
        self._stats["CacheMisses"] += 1

    def numCallsForPreprocessing(self):
        return self._stats["CallsForPreprocessing"]

    def registerCallForPreprocessing(self):
        self._stats["CallsForPreprocessing"] += 1

    def resetCounters(self):
        for k in CacheStatistics.RESETTABLE_KEYS:
            self._stats[k] = 0

    def save(self):
        self._stats.save()

    def close(self):
        self.save()


class AnalysisError(Exception):
    pass


class NoSourceFileError(AnalysisError):
    pass


class MultipleSourceFilesComplexError(AnalysisError):
    pass


class CalledForLinkError(AnalysisError):
    pass


class CalledWithPchError(AnalysisError):
    pass


class ExternalDebugInfoError(AnalysisError):
    pass


class CalledForPreprocessingError(AnalysisError):
    pass


class InvalidArgumentError(AnalysisError):
    pass


def getCompilerHash(compilerBinary):
    stat = os.stat(compilerBinary)
    data = '|'.join([
        str(stat.st_mtime),
        str(stat.st_size),
        VERSION,
        ])
    hasher = HashAlgorithm()
    hasher.update(data.encode("UTF-8"))
    return hasher.hexdigest()


def getFileHash(filePath, additionalData=None):
    hasher = HashAlgorithm()
    with open(filePath, 'rb') as inFile:
        hasher.update(inFile.read())
    if additionalData is not None:
        # Encoding of this additional data does not really matter
        # as long as we keep it fixed, otherwise hashes change.
        # The string should fit into ASCII, so UTF8 should not change anything
        hasher.update(additionalData.encode("UTF-8"))
    return hasher.hexdigest()


def expandBasedirPlaceholder(path, baseDir):
    if path.startswith(BASEDIR_REPLACEMENT):
        if not baseDir:
            raise LogicException('No CLCACHE_BASEDIR set, but found relative path ' + path)
        return path.replace(BASEDIR_REPLACEMENT, baseDir, 1)
    else:
        return path


def ensureDirectoryExists(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def copyOrLink(srcFilePath, dstFilePath):
    ensureDirectoryExists(os.path.dirname(os.path.abspath(dstFilePath)))

    if "CLCACHE_HARDLINK" in os.environ:
        ret = windll.kernel32.CreateHardLinkW(str(dstFilePath), str(srcFilePath), None)
        if ret != 0:
            # Touch the time stamp of the new link so that the build system
            # doesn't confused by a potentially old time on the file. The
            # hard link gets the same timestamp as the cached file.
            # Note that touching the time stamp of the link also touches
            # the time stamp on the cache (and hence on all over hard
            # links). This shouldn't be a problem though.
            os.utime(dstFilePath, None)
            return

    # If hardlinking fails for some reason (or it's not enabled), just
    # fall back to moving bytes around. Always to a temporary path first to
    # lower the chances of corrupting it.
    tempDst = dstFilePath + '.tmp'
    copyfile(srcFilePath, tempDst)
    os.rename(tempDst, dstFilePath)


def myExecutablePath():
    assert hasattr(sys, "frozen"), "is not frozen by py2exe"
    return sys.executable.upper()


def findCompilerBinary():
    if "CLCACHE_CL" in os.environ:
        path = os.environ["CLCACHE_CL"]
        return path if os.path.exists(path) else None

    frozenByPy2Exe = hasattr(sys, "frozen")

    for p in os.environ["PATH"].split(os.pathsep):
        path = os.path.join(p, "cl.exe")
        if os.path.exists(path):
            if not frozenByPy2Exe:
                return path

            # Guard against recursively calling ourselves
            if path.upper() != myExecutablePath():
                return path
    return None


def printTraceStatement(msg):
    if "CLCACHE_LOG" in os.environ:
        scriptDir = os.path.realpath(os.path.dirname(sys.argv[0]))
        print(os.path.join(scriptDir, "clcache.py") + " " + msg)


class CommandLineTokenizer(object):
    def __init__(self, content):
        self.argv = []
        self._content = content
        self._pos = 0
        self._token = ''
        self._parser = self._initialState

        while self._pos < len(self._content):
            self._parser = self._parser(self._content[self._pos])
            self._pos += 1

        if self._token:
            self.argv.append(self._token)

    def _initialState(self, currentChar):
        if currentChar.isspace():
            return self._initialState

        if currentChar == '"':
            return self._quotedState

        if currentChar == '\\':
            self._parseBackslash()
            return self._unquotedState

        self._token += currentChar
        return self._unquotedState

    def _unquotedState(self, currentChar):
        if currentChar.isspace():
            self.argv.append(self._token)
            self._token = ''
            return self._initialState

        if currentChar == '"':
            return self._quotedState

        if currentChar == '\\':
            self._parseBackslash()
            return self._unquotedState

        self._token += currentChar
        return self._unquotedState

    def _quotedState(self, currentChar):
        if currentChar == '"':
            return self._unquotedState

        if currentChar == '\\':
            self._parseBackslash()
            return self._quotedState

        self._token += currentChar
        return self._quotedState

    def _parseBackslash(self):
        numBackslashes = 0
        while self._pos < len(self._content) and self._content[self._pos] == '\\':
            self._pos += 1
            numBackslashes += 1

        followedByDoubleQuote = self._pos < len(self._content) and self._content[self._pos] == '"'
        if followedByDoubleQuote:
            self._token += '\\' * (numBackslashes // 2)
            if numBackslashes % 2 == 0:
                self._pos -= 1
            else:
                self._token += '"'
        else:
            self._token += '\\' * numBackslashes
            self._pos -= 1


def splitCommandsFile(content):
    return CommandLineTokenizer(content).argv


def expandCommandLine(cmdline):
    ret = []

    for arg in cmdline:
        if arg[0] == '@':
            includeFile = arg[1:]
            with open(includeFile, 'rb') as f:
                rawBytes = f.read()

            encoding = None

            encodingByBom = {
                codecs.BOM_UTF32_BE: 'utf-32-be',
                codecs.BOM_UTF32_LE: 'utf-32-le',
                codecs.BOM_UTF16_BE: 'utf-16-be',
                codecs.BOM_UTF16_LE: 'utf-16-le',
            }

            for bom, _ in list(encodingByBom.items()):
                if rawBytes.startswith(bom):
                    encoding = encodingByBom[bom]
                    rawBytes = rawBytes[len(bom):]
                    break

            if encoding:
                includeFileContents = rawBytes.decode(encoding)
            else:
                includeFileContents = rawBytes.decode("UTF-8")

            ret.extend(expandCommandLine(splitCommandsFile(includeFileContents.strip())))
        else:
            ret.append(arg)

    return ret


class Argument(object):
    def __init__(self, name):
        self.name = name

    def __len__(self):
        return len(self.name)

    def __str__(self):
        return "/" + self.name

    def __eq__(self, other):
        return type(self) == type(other) and self.name == other.name

    def __hash__(self):
        key = (type(self), self.name)
        return hash(key)


# /NAMEparameter (no space, required parameter).
class ArgumentT1(Argument):
    pass


# /NAME[parameter] (no space, optional parameter)
class ArgumentT2(Argument):
    pass


# /NAME[ ]parameter (optional space)
class ArgumentT3(Argument):
    pass


# /NAME parameter (required space)
class ArgumentT4(Argument):
    pass


class CommandLineAnalyzer(object):

    @staticmethod
    def _getParameterizedArgumentType(cmdLineArgument):
        argumentsWithParameter = {
            # /NAMEparameter
            ArgumentT1('Ob'), ArgumentT1('Yl'), ArgumentT1('Zm'),
            # /NAME[parameter]
            ArgumentT2('doc'), ArgumentT2('FA'), ArgumentT2('FR'), ArgumentT2('Fr'),
            ArgumentT2('Gs'), ArgumentT2('MP'), ArgumentT2('Yc'), ArgumentT2('Yu'),
            ArgumentT2('Zp'), ArgumentT2('Fa'), ArgumentT2('Fd'), ArgumentT2('Fe'),
            ArgumentT2('Fi'), ArgumentT2('Fm'), ArgumentT2('Fo'), ArgumentT2('Fp'),
            ArgumentT2('Wv'),
            # /NAME[ ]parameter
            ArgumentT3('AI'), ArgumentT3('D'), ArgumentT3('Tc'), ArgumentT3('Tp'),
            ArgumentT3('FI'), ArgumentT3('U'), ArgumentT3('I'), ArgumentT3('F'),
            ArgumentT3('FU'), ArgumentT3('w1'), ArgumentT3('w2'), ArgumentT3('w3'),
            ArgumentT3('w4'), ArgumentT3('wd'), ArgumentT3('we'), ArgumentT3('wo'),
            ArgumentT3('V'),
            # /NAME parameter
        }
        # Sort by length to handle prefixes
        argumentsWithParameterSorted = sorted(argumentsWithParameter, key=len, reverse=True)
        for arg in argumentsWithParameterSorted:
            if cmdLineArgument.startswith(arg.name, 1):
                return arg
        return None

    @staticmethod
    def parseArgumentsAndInputFiles(cmdline):
        arguments = defaultdict(list)
        inputFiles = []
        i = 0
        while i < len(cmdline):
            cmdLineArgument = cmdline[i]

            # Plain arguments starting with / or -
            if cmdLineArgument.startswith('/') or cmdLineArgument.startswith('-'):
                arg = CommandLineAnalyzer._getParameterizedArgumentType(cmdLineArgument)
                if arg is not None:
                    if isinstance(arg, ArgumentT1):
                        value = cmdLineArgument[len(arg) + 1:]
                        if not value:
                            raise InvalidArgumentError("Parameter for {} must not be empty".format(arg))
                    elif isinstance(arg, ArgumentT2):
                        value = cmdLineArgument[len(arg) + 1:]
                    elif isinstance(arg, ArgumentT3):
                        value = cmdLineArgument[len(arg) + 1:]
                        if not value:
                            value = cmdline[i + 1]
                            i += 1
                    elif isinstance(arg, ArgumentT4):
                        value = cmdline[i + 1]
                        i += 1
                    else:
                        raise AssertionError("Unsupported argument type.")

                    arguments[arg.name].append(value)
                else:
                    argumentName = cmdLineArgument[1:] # name not followed by parameter in this case
                    arguments[argumentName].append('')

            # Response file
            elif cmdLineArgument[0] == '@':
                raise AssertionError("No response file arguments (starting with @) must be left here.")

            # Source file arguments
            else:
                inputFiles.append(cmdLineArgument)

            i += 1

        return dict(arguments), inputFiles

    @staticmethod
    def analyze(cmdline):
        options, inputFiles = CommandLineAnalyzer.parseArgumentsAndInputFiles(cmdline)
        compl = False
        if 'Tp' in options:
            inputFiles += options['Tp']
            compl = True
        if 'Tc' in options:
            inputFiles += options['Tc']
            compl = True

        if len(inputFiles) == 0:
            raise NoSourceFileError()

        for opt in ['E', 'EP', 'P']:
            if opt in options:
                raise CalledForPreprocessingError()

        # Technically, it would be possible to support /Zi: we'd just need to
        # copy the generated .pdb files into/out of the cache.
        if 'Zi' in options:
            raise ExternalDebugInfoError()

        if 'Yc' in options or 'Yu' in options:
            raise CalledWithPchError()

        if 'link' in options or 'c' not in options:
            raise CalledForLinkError()

        if len(inputFiles) > 1 and compl:
            raise MultipleSourceFilesComplexError()

        if len(inputFiles) == 1:
            if 'Fo' in options and options['Fo'][0]:
                # Handle user input
                objectFile = os.path.normpath(options['Fo'][0])
                if os.path.isdir(objectFile):
                    objectFile = os.path.join(objectFile, basenameWithoutExtension(inputFiles[0]) + '.obj')
            else:
                # Generate from .c/.cpp filename
                objectFile = basenameWithoutExtension(inputFiles[0]) + '.obj'
        else:
            objectFile = None

        printTraceStatement("Compiler source files: {}".format(inputFiles))
        printTraceStatement("Compiler object file: {}".format(objectFile))
        return inputFiles, objectFile


def invokeRealCompiler(compilerBinary, cmdLine, captureOutput=False):
    realCmdline = [compilerBinary] + cmdLine
    printTraceStatement("Invoking real compiler as {}".format(realCmdline))

    returnCode = None
    stdout = ''
    stderr = ''
    if captureOutput:
        compilerProcess = Popen(realCmdline, stdout=PIPE, stderr=PIPE)
        stdoutBinary, stderrBinary = compilerProcess.communicate()
        stdout = stdoutBinary.decode(CL_DEFAULT_CODEC)
        stderr = stderrBinary.decode(CL_DEFAULT_CODEC)
        returnCode = compilerProcess.returncode
    else:
        returnCode = subprocess.call(realCmdline)

    printTraceStatement("Real compiler returned code {0:d}".format(returnCode))
    return returnCode, stdout, stderr


# Given a list of Popen objects, removes and returns
# a completed Popen object.
#
# This is a bit inefficient but Python on Windows does not appear to
# provide any blocking "wait for any process to complete" out of the box.
def waitForAnyProcess(procs):
    out = [p for p in procs if p.poll() is not None]
    if len(out) >= 1:
        out = out[0]
        procs.remove(out)
        return out

    # Damn, none finished yet.
    # Do a blocking wait for the first one.
    # This could waste time waiting for one process while others have
    # already finished :(
    out = procs.pop(0)
    out.wait()
    return out


# Returns the amount of jobs which should be run in parallel when
# invoked in batch mode.
#
# The '/MP' option determines this, which may be set in cmdLine or
# in the CL environment variable.
def jobCount(cmdLine):
    switches = []

    if 'CL' in os.environ:
        switches.extend(os.environ['CL'].split(' '))

    switches.extend(cmdLine)

    mpSwitches = [switch for switch in switches if re.search(r'^/MP(\d+)?$', switch) is not None]
    if len(mpSwitches) == 0:
        return 1

    # the last instance of /MP takes precedence
    mpSwitch = mpSwitches.pop()

    count = mpSwitch[3:]
    if count != "":
        return int(count)

    # /MP, but no count specified; use CPU count
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        # not expected to happen
        return 2


# Run commands, up to j concurrently.
# Aborts on first failure and returns the first non-zero exit code.
def runJobs(commands, j=1):
    running = []

    while len(commands):

        while len(running) > j:
            thiscode = waitForAnyProcess(running).returncode
            if thiscode != 0:
                return thiscode

        thiscmd = commands.pop(0)
        running.append(Popen(thiscmd))

    while len(running) > 0:
        thiscode = waitForAnyProcess(running).returncode
        if thiscode != 0:
            return thiscode

    return 0


# re-invoke clcache.py once per source file.
# Used when called via nmake 'batch mode'.
# Returns the first non-zero exit code encountered, or 0 if all jobs succeed.
def reinvokePerSourceFile(cmdLine, sourceFiles):
    printTraceStatement("Will reinvoke self for: {}".format(sourceFiles))
    commands = []
    for sourceFile in sourceFiles:
        # The child command consists of clcache.py ...
        newCmdLine = [sys.executable]
        if not hasattr(sys, "frozen"):
            newCmdLine.append(sys.argv[0])

        for arg in cmdLine:
            # and the current source file ...
            if arg == sourceFile:
                newCmdLine.append(arg)
            # and all other arguments which are not a source file
            elif arg not in sourceFiles:
                newCmdLine.append(arg)

        printTraceStatement("Child: {}".format(newCmdLine))
        commands.append(newCmdLine)

    return runJobs(commands, jobCount(cmdLine))

def printStatistics(cache):
    cfg = Configuration(cache)
    stats = CacheStatistics(cache)
    out = """
clcache statistics:
  current cache dir         : {}
  cache size                : {:,} bytes
  maximum cache size        : {:,} bytes
  cache entries             : {}
  cache hits                : {}
  cache misses
    total                      : {}
    evicted                    : {}
    header changed             : {}
    source changed             : {}
  passed to real compiler
    called w/ invalid argument : {}
    called for preprocessing   : {}
    called for linking         : {}
    called for external debug  : {}
    called w/o source          : {}
    called w/ multiple sources : {}
    called w/ PCH              : {}""".strip().format(
        cache.cacheDirectory(),
        stats.currentCacheSize(),
        cfg.maximumCacheSize(),
        stats.numCacheEntries(),
        stats.numCacheHits(),
        stats.numCacheMisses(),
        stats.numEvictedMisses(),
        stats.numHeaderChangedMisses(),
        stats.numSourceChangedMisses(),
        stats.numCallsWithInvalidArgument(),
        stats.numCallsForPreprocessing(),
        stats.numCallsForLinking(),
        stats.numCallsForExternalDebugInfo(),
        stats.numCallsWithoutSourceFile(),
        stats.numCallsWithMultipleSourceFiles(),
        stats.numCallsWithPch(),
    )
    print(out)

def resetStatistics(cache):
    with closing(CacheStatistics(cache)) as stats:
        stats.resetCounters()
    print('Statistics reset')

def cleanCache(cache):
    cfg = Configuration(cache)
    with closing(CacheStatistics(cache)) as stats:
        cache.clean(stats, cfg.maximumCacheSize())
    print('Cache cleaned')

def clearCache(cache):
    with closing(CacheStatistics(cache)) as stats:
        cache.clean(stats, 0)
    print('Cache cleared')


# Returns pair - list of includes and new compiler output.
# Output changes if strip is True in that case all lines with include
# directives are stripped from it
def parseIncludesList(compilerOutput, sourceFile, baseDir, strip):
    newOutput = []
    includesSet = set([])

    # Example lines
    # Note: including file:         C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\INCLUDE\limits.h
    # Hinweis: Einlesen der Datei:   C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\INCLUDE\iterator
    #
    # So we match
    # - one word (translation of "note")
    # - colon
    # - space
    # - a phrase containing characters and spaces (translation of "including file")
    # - colon
    # - one or more spaces
    # - the file path, starting with a non-whitespace character
    reFilePath = re.compile(r'^(\w+): ([ \w]+):( +)(?P<file_path>\S.*)$')

    absSourceFile = os.path.normcase(os.path.abspath(sourceFile))
    if baseDir:
        baseDir = os.path.normcase(baseDir)
    for line in compilerOutput.splitlines(True):
        match = reFilePath.match(line.rstrip('\r\n'))
        if match is not None:
            filePath = match.group('file_path')
            filePath = os.path.normcase(os.path.abspath(filePath))
            if filePath != absSourceFile:
                if baseDir and filePath.startswith(baseDir):
                    filePath = filePath.replace(baseDir, BASEDIR_REPLACEMENT, 1)
                includesSet.add(filePath)
        elif strip:
            newOutput.append(line)
    if strip:
        return sorted(includesSet), ''.join(newOutput)
    else:
        return sorted(includesSet), compilerOutput


def addObjectToCache(stats, cache, objectFile, compilerStdout, compilerStderr, cachekey):
    printTraceStatement("Adding file {} to cache using key {}".format(objectFile, cachekey))
    cache.setEntry(cachekey, objectFile, compilerStdout, compilerStderr)
    stats.registerCacheEntry(os.path.getsize(objectFile))
    cfg = Configuration(cache)
    cache.clean(stats, cfg.maximumCacheSize())


def processCacheHit(cache, objectFile, cachekey):
    with closing(CacheStatistics(cache)) as stats:
        stats.registerCacheHit()
    printTraceStatement("Reusing cached object for key {} for object file {}".format(cachekey, objectFile))
    if os.path.exists(objectFile):
        os.remove(objectFile)
    copyOrLink(cache.cachedObjectName(cachekey), objectFile)
    compilerOutput = cache.cachedCompilerOutput(cachekey)
    compilerStderr = cache.cachedCompilerStderr(cachekey)
    printTraceStatement("Finished. Exit code 0")
    return 0, compilerOutput, compilerStderr


def postprocessObjectEvicted(cache, objectFile, cachekey, compilerResult):
    printTraceStatement("Cached object already evicted for key {} for object {}".format(cachekey, objectFile))
    returnCode, compilerOutput, compilerStderr = compilerResult

    with cache.lock, closing(CacheStatistics(cache)) as stats:
        stats.registerEvictedMiss()
        if returnCode == 0 and os.path.exists(objectFile):
            addObjectToCache(stats, cache, objectFile, compilerOutput, compilerStderr, cachekey)

    return compilerResult


def postprocessHeaderChangedMiss(cache, objectFile, manifest, manifestHash, includesContentHash, compilerResult):
    cachekey = ObjectCache.getDirectCacheKey(manifestHash, includesContentHash)
    returnCode, compilerOutput, compilerStderr = compilerResult

    removedItems = []
    if returnCode == 0 and os.path.exists(objectFile):
        while len(manifest.includesContentToObjectMap) >= MAX_MANIFEST_HASHES:
            _, objectHash = manifest.includesContentToObjectMap.popitem()
            removedItems.append(objectHash)
        manifest.includesContentToObjectMap[includesContentHash] = cachekey

    with cache.lock, closing(CacheStatistics(cache)) as stats:
        stats.registerHeaderChangedMiss()
        if returnCode == 0 and os.path.exists(objectFile):
            addObjectToCache(stats, cache, objectFile, compilerOutput, compilerStderr, cachekey)
            cache.removeObjects(stats, removedItems)
            cache.manifestsManager.manifestSection(manifestHash).setManifest(manifestHash, manifest)

    return compilerResult


def postprocessNoManifestMiss(cache, objectFile, manifestHash, baseDir, sourceFile, compilerResult, stripIncludes):
    returnCode, compilerOutput, compilerStderr = compilerResult
    listOfIncludes, compilerOutput = parseIncludesList(compilerOutput, sourceFile, baseDir, stripIncludes)

    manifest = None
    cachekey = None

    if returnCode == 0 and os.path.exists(objectFile):
        # Store compile output and manifest
        manifest = Manifest(listOfIncludes, {})
        listOfHeaderHashes = [getFileHash(expandBasedirPlaceholder(fileName, baseDir)) for fileName in listOfIncludes]
        includesContentHash = ManifestsManager.getIncludesContentHash(listOfHeaderHashes)
        cachekey = ObjectCache.getDirectCacheKey(manifestHash, includesContentHash)
        manifest.includesContentToObjectMap[includesContentHash] = cachekey

    with cache.lock, closing(CacheStatistics(cache)) as stats:
        stats.registerSourceChangedMiss()
        if returnCode == 0 and os.path.exists(objectFile):
            # Store compile output and manifest
            addObjectToCache(stats, cache, objectFile, compilerOutput, compilerStderr, cachekey)
            cache.manifestsManager.manifestSection(manifestHash).setManifest(manifestHash, manifest)

    return returnCode, compilerOutput, compilerStderr


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--help":
        print("""
clcache.py v{}
  --help    : show this help
  -s        : print cache statistics
  -c        : clean cache
  -C        : clear cache
  -z        : reset cache statistics
  -M <size> : set maximum cache size (in bytes)
""".strip().format(VERSION))
        return 0

    cache = ObjectCache()

    if len(sys.argv) == 2 and sys.argv[1] == "-s":
        with cache.lock:
            printStatistics(cache)
        return 0

    if len(sys.argv) == 2 and sys.argv[1] == "-c":
        with cache.lock:
            cleanCache(cache)
        return 0

    if len(sys.argv) == 2 and sys.argv[1] == "-C":
        with cache.lock:
            clearCache(cache)
        return 0

    if len(sys.argv) == 2 and sys.argv[1] == "-z":
        with cache.lock:
            resetStatistics(cache)
        return 0

    if len(sys.argv) == 3 and sys.argv[1] == "-M":
        arg = sys.argv[2]
        try:
            maxSizeValue = int(arg)
        except ValueError:
            print("Given max size argument is not a valid integer: '{}'.".format(arg), file=sys.stderr)
            return 1
        if maxSizeValue < 1:
            print("Max size argument must be greater than 0.", file=sys.stderr)
            return 1

        with cache.lock, closing(Configuration(cache)) as cfg:
            cfg.setMaximumCacheSize(maxSizeValue)
        return 0

    compiler = findCompilerBinary()
    if not compiler:
        print("Failed to locate cl.exe on PATH (and CLCACHE_CL is not set), aborting.")
        return 1

    printTraceStatement("Found real compiler binary at '{0!s}'".format(compiler))
    printTraceStatement("Arguments we care about: '{}'".format(sys.argv))

    if "CLCACHE_DISABLE" in os.environ:
        return invokeRealCompiler(compiler, sys.argv[1:])[0]
    try:
        exitCode, compilerStdout, compilerStderr = processCompileRequest(cache, compiler, sys.argv)
        printBinary(sys.stdout, compilerStdout.encode(CL_DEFAULT_CODEC))
        printBinary(sys.stderr, compilerStderr.encode(CL_DEFAULT_CODEC))
        return exitCode
    except LogicException as e:
        print(e)
        return 1


def updateCacheStatistics(cache, method):
    with cache.lock, closing(CacheStatistics(cache)) as stats:
        method(stats)


def processCompileRequest(cache, compiler, args):
    printTraceStatement("Parsing given commandline '{0!s}'".format(args[1:]))

    cmdLine = expandCommandLine(args[1:])
    printTraceStatement("Expanded commandline '{0!s}'".format(cmdLine))

    try:
        sourceFiles, objectFile = CommandLineAnalyzer.analyze(cmdLine)

        if len(sourceFiles) > 1:
            return reinvokePerSourceFile(cmdLine, sourceFiles), '', ''
        else:
            assert objectFile is not None
            if 'CLCACHE_NODIRECT' in os.environ:
                return processNoDirect(cache, objectFile, compiler, cmdLine)
            else:
                return processDirect(cache, objectFile, compiler, cmdLine, sourceFiles[0])
    except InvalidArgumentError:
        printTraceStatement("Cannot cache invocation as {}: invalid argument".format(cmdLine))
        updateCacheStatistics(cache, CacheStatistics.registerCallWithInvalidArgument)
    except NoSourceFileError:
        printTraceStatement("Cannot cache invocation as {}: no source file found".format(cmdLine))
        updateCacheStatistics(cache, CacheStatistics.registerCallWithoutSourceFile)
    except MultipleSourceFilesComplexError:
        printTraceStatement("Cannot cache invocation as {}: multiple source files found".format(cmdLine))
        updateCacheStatistics(cache, CacheStatistics.registerCallWithMultipleSourceFiles)
    except CalledWithPchError:
        printTraceStatement("Cannot cache invocation as {}: precompiled headers in use".format(cmdLine))
        updateCacheStatistics(cache, CacheStatistics.registerCallWithPch)
    except CalledForLinkError:
        printTraceStatement("Cannot cache invocation as {}: called for linking".format(cmdLine))
        updateCacheStatistics(cache, CacheStatistics.registerCallForLinking)
    except ExternalDebugInfoError:
        printTraceStatement(
            "Cannot cache invocation as {}: external debug information (/Zi) is not supported".format(cmdLine)
        )
        updateCacheStatistics(cache, CacheStatistics.registerCallForExternalDebugInfo)
    except CalledForPreprocessingError:
        printTraceStatement("Cannot cache invocation as {}: called for preprocessing".format(cmdLine))
        updateCacheStatistics(cache, CacheStatistics.registerCallForPreprocessing)

    return invokeRealCompiler(compiler, args[1:])


def processDirect(cache, objectFile, compiler, cmdLine, sourceFile):
    manifestHash = ManifestsManager.getManifestHash(compiler, cmdLine, sourceFile)
    with cache.lock:
        manifest = cache.manifestsManager.manifestSection(manifestHash).getManifest(manifestHash)
        baseDir = os.environ.get('CLCACHE_BASEDIR')
        if baseDir and not baseDir.endswith(os.path.sep):
            baseDir += os.path.sep
        if manifest is not None:
            # NOTE: command line options already included in hash for manifest name
            listOfHeaderHashes = []
            for fileName in manifest.includeFiles:
                fileHash = getFileHash(expandBasedirPlaceholder(fileName, baseDir))
                if fileHash is not None:
                    # May be if source does not use this header anymore (e.g. if that
                    # header was included through some other header, which now changed).
                    listOfHeaderHashes.append(fileHash)
            includesContentHash = ManifestsManager.getIncludesContentHash(listOfHeaderHashes)
            cachekey = manifest.includesContentToObjectMap.get(includesContentHash)
            if cachekey is not None:
                if cache.hasEntry(cachekey):
                    return processCacheHit(cache, objectFile, cachekey)
                else:
                    postProcessing = lambda compilerResult: postprocessObjectEvicted(
                        cache, objectFile, cachekey, compilerResult)
            else:
                postProcessing = lambda compilerResult: postprocessHeaderChangedMiss(
                    cache, objectFile, manifest, manifestHash, includesContentHash, compilerResult)
        else:
            origCmdLine = cmdLine
            stripIncludes = False
            if '/showIncludes' not in cmdLine:
                cmdLine = ['/showIncludes'] + origCmdLine
                stripIncludes = True
            postProcessing = lambda compilerResult: postprocessNoManifestMiss(
                cache, objectFile, manifestHash, baseDir, sourceFile, compilerResult, stripIncludes)

    compilerResult = invokeRealCompiler(compiler, cmdLine, captureOutput=True)
    compilerResult = postProcessing(compilerResult)
    printTraceStatement("Finished. Exit code {0:d}".format(compilerResult[0]))
    return compilerResult


def processNoDirect(cache, objectFile, compiler, cmdLine):
    cachekey = ObjectCache.computeKey(compiler, cmdLine)
    with cache.lock:
        if cache.hasEntry(cachekey):
            return processCacheHit(cache, objectFile, cachekey)

    returnCode, compilerStdout, compilerStderr = invokeRealCompiler(compiler, cmdLine, captureOutput=True)
    with cache.lock, closing(CacheStatistics(cache)) as stats:
        stats.registerCacheMiss()
        if returnCode == 0 and os.path.exists(objectFile):
            addObjectToCache(stats, cache, objectFile, compilerStdout, compilerStderr, cachekey)

    printTraceStatement("Finished. Exit code {0:d}".format(returnCode))
    return returnCode, compilerStdout, compilerStderr

if __name__ == '__main__':
    sys.exit(main())
