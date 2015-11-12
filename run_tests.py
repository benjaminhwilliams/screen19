from __future__ import division
from libtbx import test_utils
import libtbx.load_env

def discover_unittests(module, pattern='tst_*.py'):
  try:
    import inspect
    import os
    import sys
    import unittest
  except:
    return tuple([])

  dist_dir = libtbx.env.dist_path(module)
  found_tests = unittest.defaultTestLoader.discover(dist_dir, pattern=pattern)

  def recursive_TestSuite_to_list(suite):
    list = []
    for t in suite:
      if isinstance(t, unittest.TestSuite):
        list.extend(recursive_TestSuite_to_list(t))
      elif isinstance(t, unittest.TestCase):
        module = t.__class__.__module__
        if module == 'unittest.loader':
          # This indicates a loading error.
          # Regenerate file name and try to run file directly.
          path = t._testMethodName.replace('.', os.path.sep)
          list.append("$D/%s.py" % path)
        else:
          module = inspect.getsourcefile(sys.modules[module])
          function = "%s.%s" % (t.__class__.__name__, t._testMethodName)
          list.append([module, function])
      else:
        raise Exception("Unknown test object (%s)" % t)
    return list
  test_list = recursive_TestSuite_to_list(found_tests)
  return tuple(test_list)

if (__name__ == "__main__"):
  import unittest
  test_suite = unittest.defaultTestLoader.discover(libtbx.env.dist_path("i19"), pattern="tst_*.py")
  result = unittest.TextTestRunner().run(test_suite)
  import sys
  sys.exit(0 if result.wasSuccessful() else 1)

tst_list = list(discover_unittests("i19"))
