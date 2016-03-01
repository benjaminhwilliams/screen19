from __future__ import division
import cStringIO as StringIO
import subprocess
import time
import timeit
from threading import Thread

dummy = False

class _NonBlockingStreamReader:
  '''Reads a stream in a thread to avoid blocking/deadlocks'''
  def __init__(self, stream, output=True):
    self._stream = stream
    self._buffer = StringIO.StringIO()
    self._terminated = False
    self._closed = False

    def _thread_write_stream_to_buffer():
      line = True
      while line:
        line = self._stream.readline()
        if line:
          self._buffer.write(line)
          if output:
            print line,
      self._terminated = True

    self._thread = Thread(target = _thread_write_stream_to_buffer)
    self._thread.daemon = True
    self._thread.start()

  def has_finished(self):
    return self._terminated

  def get_output(self):
    if not self.has_finished():
      raise Exception('thread did not terminate')
    if self._closed:
      raise Exception('streamreader double-closed')
    self._closed = True
    data = self._buffer.getvalue()
    self._buffer.close()
    return data

class _NonBlockingStreamWriter:
  '''Writes to a stream in a thread to avoid blocking/deadlocks'''
  def __init__(self, stream, data, debug=False):
    self._buffer = data
    self._buffer_len = len(data)
    self._buffer_pos = 0
    self._debug = debug
    self._max_block_len = 4096
    self._stream = stream
    self._terminated = False

    def _thread_write_buffer_to_stream():
      while self._buffer_pos < self._buffer_len:
        if (self._buffer_len - self._buffer_pos) > self._max_block_len:
          block = self._buffer[self._buffer_pos:(self._buffer_pos + self._max_block_len)]
        else:
          block = self._buffer[self._buffer_pos:]
        try:
          self._stream.write(block)
        except IOError, e:
          if e.errno == 32: # broken pipe, ie. process terminated without reading entire stdin
            self._stream.close()
            self._terminated = True
            return
          raise
        self._buffer_pos += len(block)
        if debug:
          print "wrote %d bytes to stream" % len(block)
      self._stream.close()
      self._terminated = True

    self._thread = Thread(target = _thread_write_buffer_to_stream)
    self._thread.daemon = True
    self._thread.start()

  def has_finished(self):
    return self._terminated

  def bytes_sent(self):
    return self._buffer_pos

  def bytes_remaining(self):
    return self._buffer_len - self._buffer_pos

def run_process(command, timeout=None, debug=False, stdin=None, print_stdout=True, print_stderr=True):
  ''' run an external process, command line specified as array,
      optionally enforces a timeout specified in seconds,
      obtains STDOUT, STDERR and exit code
      and returns summary dictionary. '''

  time_start = time.strftime("%Y-%m-%d %H:%M:%S GMT", time.gmtime())
  if debug:
    print "Starting external process:", command

  if stdin is None:
    stdin_pipe = None
  else:
    stdin_pipe = subprocess.PIPE

  if dummy:
    return { 'exitcode': 0, 'command': command,
             'stdout': '', 'stderr': '',
             'timeout': False, 'runtime': 0,
             'time_start': time_start, 'time_end': time_start }

  start_time = timeit.default_timer()
  if timeout is not None:
    max_time = start_time + timeout

  p = subprocess.Popen(command, shell=False, stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  stdout = _NonBlockingStreamReader(p.stdout, output=print_stdout)
  stderr = _NonBlockingStreamReader(p.stderr, output=print_stderr)
  if stdin is not None:
    stdin = _NonBlockingStreamWriter(p.stdin, data=stdin)

  timeout_encountered = False

  while (p.returncode is None) and \
        ((timeout is None) or (timeit.default_timer() < max_time)):
    if debug and timeout is not None:
      print "still running (T%.2fs)" % (timeit.default_timer() - max_time)

    # sleep some time
    try:
      time.sleep(0.5)
    except KeyboardInterrupt:
      p.kill() # if user pressed Ctrl+C we won't be able to produce a proper report anyway
               # but at least make sure the child process dies with us
      raise

    # check if process is still running
    p.poll()

  if p.returncode is None:
    # timeout condition
    timeout_encountered = True
    if debug:
      print "timeout (T%.2fs)" % (timeit.default_timer() - max_time)

    # send terminate signal and wait some time for buffers to be read
    p.terminate()
    time.sleep(0.5)
    if (not stdout.has_finished() or not stderr.has_finished()):
      time.sleep(2)
    p.poll()

  if p.returncode is None:
    # thread still alive
    # send kill signal and wait some more time for buffers to be read
    p.kill()
    time.sleep(0.5)
    if (not stdout.has_finished() or not stderr.has_finished()):
      time.sleep(5)
    p.poll()

  if p.returncode is None:
    raise Exception("Process won't terminate")

  runtime = timeit.default_timer() - start_time
  if debug:
    print "Process ended after %.1f seconds with exit code %d (T%.2fs)" % \
      (runtime, p.returncode, timeit.default_timer() - max_time)

  stdout = stdout.get_output()
  stderr = stderr.get_output()
  time_end = time.strftime("%Y-%m-%d %H:%M:%S GMT", time.gmtime())

  result = { 'exitcode': p.returncode, 'command': command,
             'stdout': stdout, 'stderr': stderr,
             'timeout': timeout_encountered, 'runtime': runtime,
             'time_start': time_start, 'time_end': time_end }
  if stdin is not None:
    result.update({ 'stdin_bytes_sent': stdin.bytes_sent(),
                    'stdin_bytes_remain': stdin.bytes_remaining() })

  return result
