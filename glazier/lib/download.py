# Lint as: python3
# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Download files over HTTPS.

> Resource Requirements

  * resources/ca_certs.crt
      A certificate file containing permitted root certs for SSL validation.

"""
import hashlib
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import typing
from typing import List, Optional, Text

from absl import flags
from glazier.lib import beyondcorp
from glazier.lib import file_util
from glazier.lib import winpe
from six.moves import urllib

if typing.TYPE_CHECKING:
  import http.client

CHUNK_BYTE_SIZE = 65536
SLEEP = 20

FLAGS = flags.FLAGS


def IsLocal(string: Text) -> bool:
  return re.match(r'[A-Z,a-z]\:', string) is not None


def IsRemote(string: Text) -> bool:
  return re.match(r'http(s)?:', string, re.I) is not None


def Transform(string: Text, build_info) -> Text:
  r"""Transforms abbreviated file names to absolute file paths.

  Short name support:
    #: A reference to the active release branch location.
    @: A reference to the binary storage root.
    \#: Escaped # character - replaced by # in string
    \@: Escaped @ character - replaced by @ in string

  Args:
    string: The configuration string to be transformed.
    build_info: the current build information

  Returns:
    The adjusted file name string to be used in the manifest.
  """
  string = re.sub(r'(?<!\\)#', PathCompile(build_info) + '/', string)
  string = re.sub(r'\\#', '#', string)
  string = re.sub(r'(?<!\\)@', str(build_info.BinaryPath()), string)
  string = re.sub(r'\\@', '@', string)
  return string


def PathCompile(build_info,
                file_name: Optional[Text] = None,
                base: Optional[Text] = None) -> Text:
  """Compile the active path from the base path and the active conf path.

    Attempt to do a reasonable job of joining path components with single
    slashes.

    The three main parts considered are the _base_url (or base arg), any
    subdirectories from _conf_path, and the optional file name arg.  These are
    combined into [https://base.url][/conf/path/parts][/filename.ext]

    We attempt to strip trailing slashes, so paths without a filename return
    with no trailing /.

  Args:
    build_info: the current build information
    file_name: append a filename to the path
    base: use a non-default base path

  Returns:
    The compiled URL as a string.
  """
  path = base
  if not path:
    path = build_info.ReleasePath()

  path = path.rstrip('/')

  sub_path = build_info.ActiveConfigPath()
  if sub_path:
    path += '/'
    sub_path = '/'.join(sub_path).strip('/')
    path += sub_path

  if file_name:
    path += '/'
    file_name = file_name.lstrip('/')
    path += file_name

  return path


class DownloadError(Exception):
  """The transfer of the file failed."""
  pass


class BaseDownloader(object):
  """Downloads files over HTTPS."""

  def __init__(self, show_progress: bool = False):
    self._debug_info = {}
    self._save_location = None
    self._default_show_progress = show_progress
    self._ca_cert_file = None
    self._beyondcorp = beyondcorp.BeyondCorp()

  def _ConvertBytes(self, num_bytes: int) -> Text:
    """Converts number of bytes to a human readable format.

    Args:
      num_bytes: The number to convert to a more human readable format (int).

    Returns:
      size: The number of bytes in human readable format (string).
    """
    num_bytes = float(num_bytes)
    if num_bytes >= 1099511627776:
      terabytes = num_bytes / 1099511627776
      size = '%.2fTB' % terabytes
    elif num_bytes >= 1073741824:
      gigabytes = num_bytes / 1073741824
      size = '%.2fGB' % gigabytes
    elif num_bytes >= 1048576:
      megabytes = num_bytes / 1048576
      size = '%.2fMB' % megabytes
    elif num_bytes >= 1024:
      kilobytes = num_bytes / 1024
      size = '%.2fKB' % kilobytes
    else:
      size = '%.2fB' % num_bytes
    return size

  def _GetHandlers(self):
    return [urllib.request.HTTPSHandler()]

  def _AttemptResource(self, attempt: int, max_retries: int, resource: Text):
    r"""Loop logic for retrying failed requests.

    Use logger to log messages to standard output streams, and print to write to
    console without newlines by using the return (\r) character.

    Args:
      attempt: Incrementing number of attempts.
      max_retries: Number of times to attempt to download a file if the first
        attempt fails. A negative number implies infinite.
      resource: Resource to attempt to reach.

    Raises:
      DownloadError: The resource was unreachable.
    """
    if max_retries < 0:
      logging.info(
          'Failed attempt %d of Unlimited: Sleeping for %d second(s) '
          'before retrying the %s.', attempt, SLEEP, resource)
      time.sleep(SLEEP)
    elif attempt < max_retries:
      logging.info(
          'Failed attempt %d of %d: Sleeping for %d second(s) '
          'before retrying the %s.', attempt, max_retries, SLEEP, resource)
      time.sleep(SLEEP)
    else:
      raise DownloadError('Failed to reach %s after %d attempt(s).' %
                          (resource, max_retries))

  def _OpenStream(
      self,
      url: Text,
      max_retries: int = 5,
      status_codes: Optional[List[int]] = None) -> 'http.client.HTTPResponse':
    """Opens a connection to a remote resource.

    Args:
      url:  The address of the file to be downloaded.
      max_retries:  The number of times to attempt to download a file if the
        first attempt fails. A negative number implies infinite.
      status_codes: A list of acceptable status codes to be returned by the
        remote endpoint.

    Returns:
      file_stream: urlopen's file stream

    Raises:
      DownloadError: The resource was unreachable or failed to return with the
        expected code.
    """
    attempt = 0
    file_stream = None

    opener = urllib.request.OpenerDirector()
    for handler in self._GetHandlers():
      opener.add_handler(handler)
    urllib.request.install_opener(opener)

    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
      raise DownloadError('Invalid remote server URL "%s".' % url)

    while True:
      try:
        attempt += 1
        if winpe.check_winpe():
          file_stream = urllib.request.urlopen(url, cafile=self._ca_cert_file)
        else:
          file_stream = urllib.request.urlopen(url)
      except urllib.error.HTTPError:
        logging.error('File not found on remote server: %s.', url)
      except urllib.error.URLError as e:
        logging.error(
            'Error connecting to remote server to download file '
            '"%s". The error was: %s', url, e)
        try:
          logging.info('Trying again with machine context...')
          ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
          file_stream = urllib.request.urlopen(url, context=ctx)
        except urllib.error.HTTPError:
          logging.error('File not found on remote server: %s.', url)
        except urllib.error.URLError as e:
          logging.error(
              'Error connecting to remote server to download file '
              '"%s". The error was: %s', url, e)
      if file_stream:
        if file_stream.getcode() in (status_codes or [200]):
          return file_stream
        elif file_stream.getcode() in [302]:
          url = file_stream.geturl()
        else:
          raise DownloadError('Invalid return code for file %s. [%d]' %
                              (url, file_stream.getcode()))

      self._AttemptResource(attempt, max_retries, 'file download')

  def CheckUrl(self,
               url: Text,
               status_codes: List[int],
               max_retries: int = 5) -> bool:
    """Check a remote URL for availability.

    Args:
      url: A URL to access.
      status_codes: Acceptable status codes for the connection (list).
      max_retries: Number of retries before giving up.

    Returns:
      True if accessing the file produced one of status_codes.
    """
    try:
      self._OpenStream(url, max_retries=max_retries, status_codes=status_codes)
      return True
    except DownloadError as e:
      logging.error(e)
    return False

  def DownloadFile(self,
                   url: Text,
                   save_location: Text,
                   max_retries: int = 5,
                   show_progress: bool = False):
    """Downloads a file from one location to another.

    If URL references a local path, the file will be copied rather than
    downloaded.

    Args:
      url:  The address of the file to be downloaded.
      save_location: The full path of where the file should be saved.
      max_retries:  The number of times to attempt to download a file if the
        first attempt fails.
      show_progress: Print download progress to stdout (overrides default).

    Raises:
      DownloadError: failure writing file to the save_location
    """
    self._save_location = save_location
    if IsRemote(url):
      if self._beyondcorp.CheckBeyondCorp():
        url = self._SetUrl(url)
        max_retries = -1
      file_stream = self._OpenStream(url, max_retries)
      self._StreamToDisk(file_stream, show_progress)
    else:
      try:
        file_util.Copy(url, save_location)
      except file_util.Error as e:
        raise DownloadError(str(e))

  def DownloadFileTemp(self,
                       url: Text,
                       max_retries: int = 5,
                       show_progress: bool = False) -> Text:
    """Downloads a file to temporary storage.

    Args:
      url:  The address of the file to be downloaded.
      max_retries:  The number of times to attempt to download a file if the
        first attempt fails.
      show_progress: Print download progress to stdout (overrides default).

    Returns:
      A string containing a path to the temporary file.
    """
    destination = tempfile.NamedTemporaryFile()
    self._save_location = destination.name
    destination.close()
    if self._beyondcorp.CheckBeyondCorp():
      url = self._SetUrl(url)
      max_retries = -1
    file_stream = self._OpenStream(url, max_retries)
    self._StreamToDisk(file_stream, show_progress)
    return self._save_location

  def _DownloadChunkReport(self, bytes_so_far: int, total_size: int):
    """Prints download progress information.

    Args:
      bytes_so_far:  The number of bytes downloaded so far.
      total_size:  The total size of the file being downloaded.
    """
    percent = float(bytes_so_far) / total_size
    percent = round(percent * 100, 2)
    message = (('\rDownloaded %s of %s (%0.2f%%)' +
                (' ' * 10)) % (self._ConvertBytes(bytes_so_far),
                               self._ConvertBytes(total_size), percent))
    sys.stdout.write(message)
    sys.stdout.flush()

    if bytes_so_far >= total_size:
      sys.stdout.write('\n')

  def _SetUrl(self, url: Text) -> Text:
    """Simple helper function to determine signed URL.

    Args:
      url: the url we want to download from.

    Returns:
      A string with the applicable URLs

    Raises:
      DownloadError: Failed to obtain SignedURL.
    """
    if not FLAGS.use_signed_url:
      return url
    config_server = '%s%s' % (FLAGS.config_server, '/')
    try:
      return self._beyondcorp.GetSignedUrl(
          url[url.startswith(config_server) and len(config_server):])
    except beyondcorp.BCError as e:
      raise DownloadError(e)

  def _StoreDebugInfo(self,
                      file_stream: 'http.client.HTTPResponse',
                      socket_error: Optional[Text] = None):
    """Gathers debug information for use when file downloads fail.

    Args:
      file_stream:  The file stream object of the file being downloaded.
      socket_error: Store the error raised from the socket class with other
        debug info.
    """
    if socket_error:
      self._debug_info['socket_error'] = socket_error
    if file_stream:
      for header in file_stream.info().items():
        self._debug_info[header[0]] = header[1]
    self._debug_info['current_time'] = time.strftime(
        '%A, %d %B %Y %H:%M:%S UTC')

  def PrintDebugInfo(self):
    """Print the debugging information to the screen."""
    if self._debug_info:
      print('\n\n\n\n')
      print('---------------')
      print('Debugging info: ')
      print('---------------')
      for key, value in self._debug_info.items():
        print('%s: %s' % (key, value))
      print('\n\n\n')

  def _StreamToDisk(self,
                    file_stream: 'http.client.HTTPResponse',
                    show_progress: bool = None,
                    max_retries: int = 5):
    """Save a file stream to disk.

    Args:
      file_stream: The file stream returned by a successful urlopen()
      show_progress: Print download progress to stdout (overrides default).
      max_retries:  The number of times to attempt to download a file if the
        first attempt fails. A negative number implies infinite.

    Raises:
      DownloadError: Error retrieving file or saving to disk.
    """
    progress = self._default_show_progress
    if show_progress is not None:
      progress = show_progress

    bytes_so_far = 0
    attempt = 0
    while True:
      attempt += 1
      try:
        url = file_stream.geturl()
        total_size = int(file_stream.headers.get('Content-Length').strip())
        break
      except AttributeError:
        self._AttemptResource(attempt, max_retries, 'server URL')

    try:
      with open(self._save_location, 'wb') as output_file:
        logging.info('Downloading file "%s" to "%s".', url, self._save_location)
        while 1:
          chunk = file_stream.read(CHUNK_BYTE_SIZE)
          bytes_so_far += len(chunk)
          if not chunk:
            break
          output_file.write(chunk)
          if progress:
            self._DownloadChunkReport(bytes_so_far, total_size)
    except socket.error as e:
      self._StoreDebugInfo(file_stream, str(e))
      raise DownloadError('Socket error during download.')
    except IOError:
      raise DownloadError('File location could not be opened for writing: %s' %
                          self._save_location)
    self._Validate(file_stream, total_size)
    file_stream.close()

  def _Validate(self, file_stream: 'http.client.HTTPResponse',
                expected_size: int):
    """Validate the downloaded file.

    Args:
      file_stream: The file stream returned by a successful urlopen()
      expected_size:  The total size of the file being downloaded.

    Raises:
      DownloadError: File failed validation.
    """
    if not os.path.exists(self._save_location):
      self._StoreDebugInfo(file_stream)
      raise DownloadError('Could not locate file at %s' % self._save_location)

    actual_file_size = os.path.getsize(self._save_location)
    if actual_file_size != expected_size:
      self._StoreDebugInfo(file_stream)
      message = ('File size of %s bytes did not match expected size of %s!' %
                 (actual_file_size, expected_size))
      raise DownloadError(message)

  def VerifyShaHash(self, file_path: Text, expected: Text) -> bool:
    """Verifies the SHA256 hash of a file.

    Arguments:
      file_path: The path to the file that will be checked.
      expected: The expected SHA hash as a string.

    Returns:
      True if the calculated hash matches the expected hash.
      False if the calculated hash does not match the expected hash or if there
          was an error reading the file or the SHA file.
    """
    sha_object = hashlib.new('sha256')

    # Read the file in 4MB chunks to avoid running out of memory
    # while processing very large files.
    try:
      with open(file_path, 'rb') as f:
        while True:
          current_chunk = f.read(4194304)
          if not current_chunk:
            break
          sha_object.update(current_chunk)
    except IOError:
      logging.error('Unable to read file %s for SHA verification.', file_path)
      return False

    file_hash = sha_object.hexdigest()
    expected = expected.lower()

    if file_hash == expected:
      logging.info('SHA256 hash for %s matched expected hash of %s.', file_path,
                   expected)
      return True
    else:
      logging.error(
          'SHA256 hash for %s was %s, which did not match expected hash of %s.',
          file_path, file_hash, expected)
      return False


# Set our downloader of choice
Download = BaseDownloader
