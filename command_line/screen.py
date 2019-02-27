#!/usr/bin/env python
# -*- coding: utf-8 -*-

u"""
Process screening data obtained at Diamond Light Source Beamline I19.

This program presents the user with recommendations for adjustments to beam
flux, based on a single-sweep screening data collection.  It presents an
upper- and lower-bound estimate of suitable flux.
  * The upper-bound estimate is based on a comparison of a histogram of
  measured pixel intensities with the trusted intensity range of the detector.
  The user is warned when the measured pixel intensities indicate that the
  detector would have a significant number of overloaded or untrustworthy
  pixels.
  * The lower-bound estimate is based on a linear fit of isotropic disorder
  parameter, B, to a Wilson plot of reflection intensities.  From this,
  an estimate is made of the minimum flux required to achieve a target I/σ
  ratio (by default, target I/σ = 2) at one or more values of desired
  resolution, d, (by default, desired d = 1 Å, 0.84 Å, 0.6 Å & 0.4 Å).

Target I/σ and target d (in Ångström) can be set using the parameters
'min_i_over_sigma' and 'desired_d'.  One can set multiple values of the latter.

By default the disorder parameter fit is conducted on the
integrated data.  This ought to provide a reasonably true fit, but requires
an integration step, which can take some time.  You can achieve a quicker,
dirtier answer by fitting to the indexed data (i.e. only the stronger
spots), using 'i19_minimum_flux.data=indexed'.

Examples:

  i19.screen datablock.json

  i19.screen *.cbf

  i19.screen /path/to/data/

  i19.screen /path/to/data/image0001.cbf:1:100

  i19.screen min_i_over_sigma=2 desired_d=0.84 <datablock.json | image_files>

  i19.screen i19_minimum_flux.data=indexed <image_files>

"""

from __future__ import absolute_import, division, print_function

import json
import logging

from typing import Dict, List, Tuple, Optional

import math
import os
import re
import sys
import time
import timeit

import procrunner
import iotbx.phil
from libtbx import Auto
from dxtbx.model.experiment_list import ExperimentListFactory
from dials.util.options import OptionParser
from i19.command_line import prettyprint_dictionary, make_template, plot_intensities


help_message = __doc__

phil_scope = iotbx.phil.parse(
    '''
verbosity = 1
  .type = int(value_min=0)
  .caption = 'The verbosity level of the command-line output'
  .help = """
Possible values:
    * 0: Suppress all command-line output;
    * 1: Show regular output on the command line;
    * 2: Show regular output, plus detailed debugging messages.
"""

output
  .caption = 'Options to control the output files'
  {
  log = 'i19.screen.log'
  .type = str
  .caption = "The log filename"
  .help = 'If False, no info log will be created.'

  debug_log = 'i19.screen.debug.log'
  .type = str
  .caption = "The debug log filename"
  .help = 'If False, no debug log will be created.'
  }

nproc = Auto
  .type = int
  .caption = 'Number of processors to use'
  .help = 'The chosen value will apply to all the DIALS utilities with a ' \
          'multi-processing option.  If 'False' or 'Auto', all available ' \
          'processors will be used.'

i19_minimum_flux
  .caption = 'Options for i19.minimum_flux'
  {
  include scope i19.command_line.minimum_flux.phil_scope
  data = indexed *integrated
    .type = choice
    .caption = 'Choice of data for the displacement parameter fit'
    .help = 'For the lower-bound flux estimate, choose whether to use ' \
            'indexed (quicker) or integrated (better) data in fitting ' \
            'the isotropic displacement parameter.'
  }

dials_import
  .caption = 'Options for dials.import'
  {
  include scope dials.command_line.dials_import.phil_scope
  }

dials_find_spots
  .caption = 'Options for dials.find_spots'
  {
  include scope dials.command_line.find_spots.phil_scope
  }

dials_index
  .caption = 'Options for dials.index'
  {
  include scope dials.command_line.index.phil_scope
  }

dials_refine
  .caption = 'Options for dials.refine'
  {
  include scope dials.command_line.refine.phil_scope
  }

dials_refine_bravais
  .caption = 'Options for dials.refine_bravais_settings'
  {
  include scope dials.command_line.refine_bravais_settings.phil_scope
  }

dials_create_profile
  .caption = 'Options for dials.create_profile_model'
  {
  include scope dials.command_line.create_profile_model.phil_scope
  }

dials_integrate
  .caption = 'Options for dials.integrate'
  {
  include scope dials.command_line.integrate.phil_scope
  }

dials_report
  .caption = 'Options for dials.report'
  {
  include scope dials.command_line.report.phil_scope
  }
''',
    process_includes=True,
)

procrunner_debug = False

logger = logging.getLogger("dials.i19.screen")
debug, info, warn = logger.debug, logger.info, logger.warn


def terminal_size():
    """
    Find the current size of the terminal window.

    :return: Number of columns; number of rows.
    :rtype: Tuple[int]
    """
    columns, rows = 80, 25
    if sys.stdout.isatty():
        try:
            result = procrunner.run(
                ["stty", "size"],
                timeout=1,
                print_stdout=False,
                print_stderr=False,
                debug=procrunner_debug,
            )
            rows, columns = [int(i) for i in result["stdout"].split()]
        except Exception:  # ignore any errors and use default size
            pass  # FIXME: Can we be more specific about the type of exception?
    columns = min(columns, 120)
    rows = min(rows, int(columns / 3))

    return columns, rows


class I19Screen(object):
    """
    Encapsulates the screening script.
    """

    # TODO Make __init__ and declare instance variables in it.
    def _quick_import(self, files):
        """
        TODO: Docstring
        :param files:
        :type files: List[str]
        :return:
        """
        if len(files) == 1:
            # No point in quick-importing a single file
            return False
        debug("Attempting quick import...")
        files.sort()
        templates = {}  # type: Dict[str, List[Optional[List[int]]]]
        for f in files:
            template, image = make_template(f)
            if template not in templates:
                image_range = [image, image] if image else []
                templates.update({template: [image_range]})
            elif image == templates[template][-1][-1] + 1:
                templates[template][-1][-1] = image
            else:
                templates[template].append([image, image])
        # Return tuple of template and image range for each unique image range
        templates = [(t, tuple(r)) for t, ranges in templates.items() for r in ranges]
        # type: List[Tuple[str, Tuple[int]]]
        return self._quick_import_templates(templates)

    def _quick_import_templates(self, templates):
        """
        TODO: Docstring
        :param templates:
        :return:
        """
        debug("Quick import template summary:\n\t%s", templates)
        if len(templates) > 1:
            debug("Cannot currently run quick import on multiple templates")
            return False

        try:
            scan_range = templates[0][1]  # type: Tuple[int]
            if not scan_range:
                raise IndexError
        except IndexError:
            debug(
                "Cannot run quick import: could not determine image naming template"
            )
            return False

        info("Running quick import")
        self._run_dials_import(
            [
                "input.template=%s" % templates[0][0],
                "geometry.scan.image_range=%d,%d" % scan_range,
                "geometry.scan.extrapolate_scan=True",
            ]
        )
        return True

    def _import(self, files):
        """
        TODO: Docstring
        :param files:
        :return:
        """
        info("\nImporting data...")
        if len(files) == 1:
            if os.path.isdir(files[0]):
                debug(
                    "You specified a directory. Importing all CBF files in "
                    "that directory."
                )
                # TODO Support other image formats for more general application
                files = [
                    os.path.join(files[0], f)
                    for f in os.listdir(files[0])
                    if f.endswith(".cbf")
                ]
            elif len(files[0].split(":")) == 3:
                debug(
                    "You specified an image range in the xia2 format.  "
                    "Importing all specified files."
                )
                template, start, end = files[0].split(":")
                template = make_template(template)[0]
                start, end = int(start), int(end)
                if not self._quick_import_templates([(template, (start, end))]):
                    warn("Could not import specified image range.")
                    sys.exit(1)
                info("Quick import successful")
                return

        # Can the files be quick-imported?
        if self._quick_import(files):
            info("Quick import successful")
            return

        self._run_dials_import(files)

    def _run_dials_import(self, parameters):
        """
        TODO: Docstring
        :param parameters:
        :return:
        """
        from dials.command_line.dials_import import Script as ImportScript

        # Get the dials.import master scope
        dials_import_master = iotbx.phil.parse(
            "include scope dials.command_line.dials_import.phil_scope",
            process_includes=True,
        )
        # Combine this with the working scope from the command-line input,
        # having preference for the working scope where they differ
        import_scope = dials_import_master.format(self.params.dials_import)
        # Set up the dials.import script with these phil parameters
        import_script = ImportScript(phil=import_scope)
        # Run the script, suppressing stdout.
        # TODO parameters += ['allow_multiple_sweeps=True']
        try:
            import_script.run(parameters)
        except SystemExit as e:
            if e.code:
                warn("dials.import failed with exit code %d", e.code)
                sys.exit(1)

    def _count_processors(self, nproc=None):
        """
        Determine the number of processors and save it as an instance variable.

        The user may specify the number of processors to use.  If no value is
        given, the number of available processors is returned.

        :param nproc: User-specified number of processors to use.
        :type nproc: int
        """
        if nproc and nproc is not Auto:
            self.nproc = nproc
            return

        # if environmental variable NSLOTS is set to a number then use that
        try:
            self.nproc = int(os.environ.get("NSLOTS"))
            return
        except (ValueError, TypeError):
            pass

        from libtbx.introspection import number_of_processors

        self.nproc = number_of_processors(return_value_if_unknown=-1)

        if self.nproc <= 0:
            warn(
                "Could not determine number of available processors. Error code %d",
                self.nproc,
            )
            sys.exit(1)

    def _count_images(self):
        """
        Attempt to determine the number of diffraction images.

        The number of diffraction images is determined from the datablock JSON
        file.

        :return: Number of images.
        :rtype: int
        """
        with open(self.json_file) as fh:
            datablock = json.load(fh)
        try:
            return sum(len(s["exposure_time"]) for s in datablock[0]["scan"])
        except Exception:  # FIXME: Can we be specific?
            warn("Could not determine number of images in dataset")
            sys.exit(1)

    def _check_intensities(self, mosaicity_correction=True):
        """
        TODO: Docstring
        :param mosaicity_correction:
        :return:
        """
        info("\nTesting pixel intensities...")
        command = ["xia2.overload", "nproc=%s" % self.nproc, self.json_file]
        debug("running %s", command)
        result = procrunner.run(command, print_stdout=False, debug=procrunner_debug)
        debug("result = %s", prettyprint_dictionary(result))
        info("Successfully completed (%.1f sec)", result["runtime"])

        if result["exitcode"] != 0:
            warn("Failed with exit code %d", result["exitcode"])
            sys.exit(1)

        with open("overload.json") as fh:
            overload_data = json.load(fh)

        print("Pixel intensity distribution:")
        count_sum = 0
        hist = {}
        if "bins" in overload_data:
            for b in range(overload_data["bin_count"]):
                if overload_data["bins"][b] > 0:
                    hist[b] = overload_data["bins"][b]
                    count_sum += b * overload_data["bins"][b]
        else:
            hist = {int(k): v for k, v in overload_data["counts"].items() if int(k) > 0}
            count_sum = sum([k * v for k, v in hist.items()])

        average_to_peak = 1
        if mosaicity_correction:
            # we have checked this: if sigma_m >> oscillation it works out
            # about 1 as you would expect
            if self._sigma_m:
                M = (
                    math.sqrt(math.pi)
                    * self._sigma_m
                    * math.erf(self._oscillation / (2 * self._sigma_m))
                )
                average_to_peak = M / self._oscillation
                info("Average-to-peak intensity ratio: %f", average_to_peak)

        scale = 100 * overload_data["scale_factor"] / average_to_peak
        info("Determined scale factor for intensities as %f", scale)

        debug(
            "intensity histogram: { %s }",
            ", ".join(["%d:%d" % (k, hist[k]) for k in sorted(hist)]),
        )
        max_count = max(hist.keys())
        hist_max = max_count * scale
        hist_granularity, hist_format = 1, "%.0f"
        if hist_max < 50:
            hist_granularity, hist_format = 2, "%.1f"
        if hist_max < 15:
            hist_granularity, hist_format = 10, "%.1f"
        rescaled_hist = {}
        for x in hist.keys():
            rescaled = round(x * scale * hist_granularity)
            if rescaled > 0:
                rescaled_hist[rescaled] = hist[x] + rescaled_hist.get(rescaled, 0)
        hist = rescaled_hist
        debug(
            "rescaled histogram: { %s }",
            ", ".join(
                [
                    (hist_format + ":%d") % (k / hist_granularity, hist[k])
                    for k in sorted(hist)
                ]
            ),
        )

        plot_intensities(hist, 1 / hist_granularity, procrunner_debug=procrunner_debug)

        text = "".join(
            (
                "Strongest pixel (%d counts) " % max_count,
                "reaches %.1f%% " % hist_max,
                "of the detector count rate limit",
            )
        )
        if hist_max > 100:
            warn("Warning: %s!", text)
        else:
            info(text)
        if (
            "overload_limit" in overload_data
            and max_count >= overload_data["overload_limit"]
        ):
            warn(
                "Warning: THE DATA CONTAIN REGULAR OVERLOADS!\n"
                "         The photon incidence rate is outside the specified "
                "limits of the detector.\n"
                "         The built-in detector count rate correction cannot "
                "adjust for this.\n"
                "         You should aim for count rates below 25% of the "
                "detector limit."
            )
        elif hist_max > 70:
            warn(
                "Warning: The photon incidence rate is well outside the "
                "linear response region of the detector (<25%).\n"
                "    The built-in detector count rate correction may not be "
                "able to adjust for this."
            )
        elif hist_max > 25:
            info(
                "The photon incidence rate is outside the linear response "
                "region of the detector (<25%).\n"
                "The built-in detector count rate correction should be able "
                "to adjust for this."
            )
        if not mosaicity_correction:
            warn(
                "Warning: Not enough data for proper profile estimation."
                "    The spot intensities are not corrected for mosaicity.\n"
                "    The true photon incidence rate will be higher than the "
                "given estimate."
            )

        info("Total sum of counts in dataset: %d", count_sum)

    # TODO Introduce a dials.generate_mask call

    def _find_spots(self, args=None):
        """
        TODO: Docstring
        :param additional_parameters:
        :type additional_parameters: List[str]
        :return:
        """
        info("\nFinding spots...")

        dials_start = timeit.default_timer()

        if not args:
            args = []

        from dials.command_line.find_spots import Script as SpotFinderScript

        # Set the input file
        args = [self.json_file] + args
        # Get the dials.find_spots master scope
        find_spots_master = iotbx.phil.parse(
            "include scope dials.command_line.find_spots.phil_scope",
            process_includes=True,
        )
        # Combine this with the working scope from the command-line input,
        # having preference for the working scope where they differ
        find_spots_scope = find_spots_master.format(self.params.dials_find_spots)
        # Set up the dials.find_spots script with these phil parameters
        finder_script = SpotFinderScript(phil=find_spots_scope)
        # Run the script
        try:
            if self.params.dials_find_spots.output.datablock:
                expts, refls = finder_script.run(args)
            else:
                refls = finder_script.run(args)
        except SystemExit as e:
            if e.code:
                warn("dials.find_spots failed with exit code %d", e.code)
                sys.exit(1)

        from dials.util.ascii_art import spot_counts_per_image_plot

        info(
            60 * "-" + "\n%s\n" + 60 * "-" + "\nSuccessfully completed (%.1f sec)",
            spot_counts_per_image_plot(refls),
            timeit.default_timer() - dials_start,
        )

    def _index(self):
        """
        TODO: Docstring
        :return:
        """
        dials_start = timeit.default_timer()

        from dials.command_line import index

        # Set the input files
        basic_args = [
            self.params.dials_import.output.datablock,
            self.params.dials_find_spots.output.reflections,
        ]

        # Get the dials.index master scope
        index_master = iotbx.phil.parse(
            "include scope dials.command_line.index.phil_scope", process_includes=True
        )
        # Combine this with the working scope from the command-line input,
        # having preference for the working scope where they differ
        index_scope = index_master.format(self.params.dials_index)

        runlist = [
            ("Indexing", []),
            ("Retrying with max_cell constraint", ["max_cell=20"]),
            ("Retrying with 1D FFT", ["indexing.method=fft1d"]),
        ]

        for message, args in runlist:
            info("\n%s...", message)
            try:
                # Run indexing and get the indexer object
                expts, refls = index.run(phil=index_scope, args=basic_args + args)
                sys.exit(0)
            except SystemExit as e:
                if e.code == 0:
                    break
                else:
                    warn("Failed with exit code %d", e)
        else:
            return False

        sg_type = expts[0].crystal.get_crystal_symmetry().space_group().type()
        symb = sg_type.universal_hermann_mauguin_symbol()
        unit_cell = expts[0].crystal.get_unit_cell()

        info(
            "Found primitive solution: %s %s using %s reflections\n"
            "Successfully completed (%.1f sec)",
            symb,
            unit_cell,
            refls["id"].count(0),
            timeit.default_timer() - dials_start,
        )

        return True

    def _wilson_calculation(self, experiments, reflections):
        """
        TODO: Docstring

        :param experiments:
        :param reflections:
        :return:
        """
        dials_start = timeit.default_timer()
        info("\nEstimating lower flux bound...")

        from i19.command_line import minimum_flux

        # Get the i19.minimum_flux master scope
        min_flux_master = iotbx.phil.parse(
            "include scope i19.command_line.minimum_flux.phil_scope",
            process_includes=True,
        )
        # Combine this with the working scope from the command-line input,
        # having preference for the working scope where they differ
        min_flux_scope = min_flux_master.format(self.params.i19_minimum_flux)

        args = [experiments, reflections]

        try:
            # Run i19.minimum_flux
            minimum_flux.run(phil=min_flux_scope, args=args)
        except SystemExit as e:
            if e.code:
                warn("i19.minimum_flux failed with exit code %d\nGiving up.", e.code)
                sys.exit(1)

        info("Successfully completed (%.1f sec)", timeit.default_timer() - dials_start)

    def _refine(self):
        """
        TODO: Docstring
        :return:
        """
        dials_start = timeit.default_timer()
        info("\nRefining...")

        from dials.command_line.refine import Script

        os.rename(
            self.params.dials_index.output.experiments, "experiments_unrefined.json"
        )
        os.rename(
            self.params.dials_index.output.reflections, "indexed_unrefined.pickle"
        )
        args = ["experiments_unrefined.json", "indexed_unrefined.pickle"]
        self.params.dials_refine.output.experiments = (
            self.params.dials_index.output.experiments
        )
        self.params.dials_refine.output.reflections = (
            self.params.dials_index.output.reflections
        )

        # Get the dials.refine master scope
        refine_master = iotbx.phil.parse(
            "include scope dials.command_line.refine.phil_scope", process_includes=True
        )
        # Combine this with the working scope from the command-line input,
        # having preference for the working scope where they differ
        refine_scope = refine_master.format(self.params.dials_refine)
        # Set up the dials.refine script
        refine = Script(phil=refine_scope)

        try:
            # Run dials.refine
            refine.run(args)
        except SystemExit as e:
            if e.code:
                warn("dials.refine failed with exit code %d\nGiving up.", e.code)
                sys.exit(1)

        info("Successfully refined (%.1f sec)", timeit.default_timer() - dials_start)

    def _create_profile_model(self):
        """
        TODO: Docstring
        :return:
        """
        info("\nCreating profile model...")
        command = ["dials.create_profile_model", "experiments.json", "indexed.pickle"]
        result = procrunner.run(command, print_stdout=False, debug=procrunner_debug)
        debug("result = %s", prettyprint_dictionary(result))
        self._sigma_m = None
        if result["exitcode"] == 0:
            db = ExperimentListFactory.from_json_file(
                "experiments_with_profile_model.json"
            )[0]
            self._num_images = db.imageset.get_scan().get_num_images()
            self._oscillation = db.imageset.get_scan().get_oscillation()[1]
            self._sigma_m = db.profile.sigma_m()
            info(
                "%d images, %s deg. oscillation, sigma_m=%.3f",
                self._num_images,
                str(self._oscillation),
                self._sigma_m,
            )
            info("Successfully completed (%.1f sec)", result["runtime"])
            return True
        warn("Failed with exit code %d", result["exitcode"])
        return False

    def _integrate(self):
        """
        TODO: Docstring
        :return:
        """
        dials_start = timeit.default_timer()
        info("\nIntegrating...")

        from dials.command_line.integrate import Script

        args = [
            self.params.dials_index.output.experiments,
            self.params.dials_index.output.reflections,
        ]

        # Get the dials.integrate master scope
        integrate_master = iotbx.phil.parse(
            "include scope dials.command_line.integrate.phil_scope",
            process_includes=True,
        )
        # Retain shoeboxes in order to determine reflections containing overloads
        self.params.dials_integrate.integration.debug.output = True
        self.params.dials_integrate.integration.debug.delete_shoeboxes = False
        self.params.dials_integrate.integration.debug.separate_files = False
        # Combine this with the working scope from the command-line input,
        # having preference for the working scope where they differ
        integrate_scope = integrate_master.format(self.params.dials_integrate)
        # Set up the dials.integrate script
        integrate = Script(phil=integrate_scope)

        try:
            # Run dials.refine
            integrated_experiments, integrated = integrate.run(args)
        except SystemExit as e:
            if e.code:
                warn("dials.refine failed with exit code %d\nGiving up.", e.code)
                sys.exit(1)
            else:
                info(
                    "Successfully completed (%.1f sec)",
                    timeit.default_timer() - dials_start,
                )

        return integrated_experiments, integrated

    def _find_overloads(self, integrated_experiments, integrated):
        """
        TODO: Docstring

        :return:
        """
        from dials.array_family import flex

        # Select those reflections having total summed intensity greater than
        # 0.25 × the upper limit of the trusted range (that being the in-house limit)
        # TODO: Make this limit not hard coded, based instead on dxtbx detector info
        detector = integrated_experiments[0].detector.to_dict()
        # Assumes all panels have same trusted range, presumably this isn't far-fetched
        upper_limit = .25 * detector['panels'][0]['trusted_range'][1]
        sel = (integrated['intensity.sum.value'] > upper_limit).iselection()
        strongest = integrated.select(sel)

        # Check the pixel values of all the high-intensity spots for pixel overloads
        overloads = flex.bool(
            [any(shoebox.values() > upper_limit) for shoebox in strongest['shoebox']]
        ).iselection()

        # Flag spots with overloaded pixels — also flag them for exclusion from scaling
        bad_flag = strongest.flags.overloaded | strongest.flags.excluded_for_scaling
        strongest.set_flags(overloads, bad_flag)
        integrated.set_selected(sel, strongest)

        # Delete the shoeboxes and overwrite the reflection table on disk, to save space
        del integrated['shoebox']
        integrated.as_pickle(self.params.dials_integrate.output.reflections)

        # Return the number of reflections containing overloaded pixels
        return overloads.size()

    def _refine_bravais(self):
        """
        TODO: Docstring
        :return:
        """
        info("\nRefining bravais settings...")
        command = [
            "dials.refine_bravais_settings",
            "experiments.json",
            "indexed.pickle",
        ]
        result = procrunner.run(command, print_stdout=False, debug=procrunner_debug)
        debug("result = %s", prettyprint_dictionary(result))
        if result["exitcode"] == 0:
            m = re.search("---+\n[^\n]*\n---+\n(.*\n)*---+", result["stdout"])
            info(m.group(0))
            info("Successfully completed (%.1f sec)", result["runtime"])
        else:
            warn("Failed with exit code %d", result["exitcode"])
            sys.exit(1)

    def _report(self):
        """
        TODO: Docstring
        :return:
        """
        info("\nCreating report...")
        command = [
            "dials.report",
            "experiments_with_profile_model.json",
            "indexed.pickle",
        ]
        result = procrunner.run(command, print_stdout=False, debug=procrunner_debug)
        debug("result = %s", prettyprint_dictionary(result))
        if result["exitcode"] == 0:
            info("Successfully completed (%.1f sec)", result["runtime"])
        #     if sys.stdout.isatty():
        #       info("Trying to start browser")
        #       try:
        #         import subprocess
        #         d = dict(os.environ)
        #         d["LD_LIBRARY_PATH"] = ""
        #         subprocess.Popen(["xdg-open", "dials-report.html"], env=d)
        #       except Exception as e:
        #         debug("Could not open browser\n%s", str(e))
        else:
            warn("Failed with exit code %d", result["exitcode"])
            sys.exit(1)

    def run(self, args=None, phil=phil_scope):
        """
        TODO: Docstring
        :param args:
        :param phil:
        :return:
        """
        import libtbx.load_env
        from i19.util.version import i19_version
        from dials.util.version import dials_version

        usage = (
            "%s [options] image_directory | image_files.cbf | "
            "datablock.json" % libtbx.env.dispatcher_name
        )

        parser = OptionParser(
            usage=usage, epilog=help_message, phil=phil, check_format=False
        )

        self.params, options, unhandled = parser.parse_args(
            args=args, show_diff_phil=True, return_unhandled=True, quick_parse=True
        )

        version_information = "%s using %s (%s)" % (
            i19_version(),
            dials_version(),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        start = timeit.default_timer()

        if len(unhandled) == 0:
            print(help_message)
            print(version_information)
            return

        if __name__ == "__main__":
            from dials.util import log

            # Configure the logging
            log.config(
                self.params.verbosity,
                info=self.params.output.log,
                debug=self.params.output.debug_log,
            )
            # Filter the handlers for the info log file and stdout,
            # such that no child log records from DIALS scripts end up
            # there.  Retain child log records in the debug log file.
            for handler in logging.getLogger("dials").handlers:
                if handler.name in ("stream", "file_info"):
                    handler.addFilter(logging.Filter("dials.i19"))

        info(version_information)
        debug("Run with:\n%s\n%s", " ".join(unhandled), parser.diff_phil.as_str())

        self._count_processors(nproc=self.params.nproc)
        debug("Using %s processors", self.nproc)
        # Set multiprocessing settings for spot-finding, indexing and
        # integration to match the top-level specified number of processors
        self.params.dials_find_spots.spotfinder.mp.nproc = self.nproc
        self.params.dials_index.indexing.nproc = self.nproc
        # Setting self.params.dials_refine.refinement.mp.nproc is not helpful
        self.params.dials_integrate.integration.mp.nproc = self.nproc

        # Set the input and output parameters for the DIALS components
        # TODO: Compare to diff_phil and start from later in the pipeline if
        #  appropriate
        if len(unhandled) == 1 and unhandled[0].endswith(".json"):
            self.json_file = unhandled[0]
        else:
            self.json_file = "datablock.json"
            self.params.dials_import.output.datablock = self.json_file
            self._import(unhandled)

        n_images = self._count_images()
        fast_mode = n_images < 10
        if fast_mode:
            info("%d images found, skipping a lot of processing", n_images)

        self._find_spots()
        if not self._index():
            info("\nRetrying for stronger spots only...")
            os.rename("strong.pickle", "all_spots.pickle")
            self._find_spots(["sigma_strong=15"])
            if not self._index():
                warn("Giving up.")
                info(
                    "Could not find an indexing solution. You may want to "
                    "have a look at the reciprocal space by running:\n\n"
                    "    dials.reciprocal_lattice_viewer datablock.json "
                    "all_spots.pickle\n\n"
                    "or, to only include stronger spots:\n\n"
                    "    dials.reciprocal_lattice_viewer datablock.json "
                    "strong.\n"
                )
                sys.exit(1)

        if not fast_mode and not self._create_profile_model():
            info("\nRefining model to attempt to increase number of valid spots...")
            self._refine()
            if not self._create_profile_model():
                warn("Giving up.")
                info(
                    "The identified indexing solution may not be correct. "
                    "You may want to have a look at the reciprocal space by "
                    "running:\n\n"
                    "    dials.reciprocal_lattice_viewer experiments.json "
                    "indexed.pickle\n"
                )
                sys.exit(1)

        if self.params.i19_minimum_flux.data == "integrated":
            integrated_experiments, integrated = self._integrate()
            num_overloaded = self._find_overloads(integrated_experiments, integrated)
            if num_overloaded:
                info(
                    '%d reflections contain overloaded pixels and are excluded from '
                    'further processing.',
                    num_overloaded
                )
            else:
                info('No reflections contain overloaded pixels.')

            self._wilson_calculation(
                self.params.dials_integrate.output.experiments,
                self.params.dials_integrate.output.reflections,
            )
        else:
            self._wilson_calculation(
                self.params.dials_index.output.experiments,
                self.params.dials_index.output.reflections,
            )

        if not fast_mode:
            self._check_intensities()
            self._report()

        self._refine_bravais()

        i19screen_runtime = timeit.default_timer() - start
        debug(
            "Finished at %s, total runtime: %.1f",
            time.strftime("%Y-%m-%d %H:%M:%S"),
            i19screen_runtime,
        )
        info("i19.screen successfully completed (%.1f sec)", i19screen_runtime)


if __name__ == "__main__":
    from dials.util.version import dials_version
    if dials_version().startswith("DIALS 1.12."):
        from i19.util.screen_legacy import I19Screen
    I19Screen().run()
