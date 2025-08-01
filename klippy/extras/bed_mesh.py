# Mesh Bed Leveling
#
# Copyright (C) 2018-2019 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import math
import json
import collections
from . import probe

PROFILE_VERSION = 1
PROFILE_OPTIONS = {
    "min_x": float,
    "max_x": float,
    "min_y": float,
    "max_y": float,
    "x_count": int,
    "y_count": int,
    "mesh_x_pps": int,
    "mesh_y_pps": int,
    "algo": str,
    "tension": float,
}


class BedMeshError(Exception):
    pass


# PEP 485 isclose()
def isclose(a, b, rel_tol=1e-09, abs_tol=0.0):
    return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)


# return true if a coordinate is within the region
# specified by min_c and max_c
def within(coord, min_c, max_c, tol=0.0):
    return (max_c[0] + tol) >= coord[0] >= (min_c[0] - tol) and (
        max_c[1] + tol
    ) >= coord[1] >= (min_c[1] - tol)


# Constrain value between min and max
def constrain(val, min_val, max_val):
    return min(max_val, max(min_val, val))


# Linear interpolation between two values
def lerp(t, v0, v1):
    return (1.0 - t) * v0 + t * v1


# retrieve comma separated pair from config
def parse_config_pair(config, option, default, minval=None, maxval=None):
    pair = config.getintlist(option, (default, default))
    if len(pair) != 2:
        if len(pair) != 1:
            raise config.error(
                "bed_mesh: malformed '%s' value: %s" % (option, config.get(option))
            )
        pair = (pair[0], pair[0])
    if minval is not None:
        if pair[0] < minval or pair[1] < minval:
            raise config.error(
                "Option '%s' in section bed_mesh must have a minimum of %s"
                % (option, str(minval))
            )
    if maxval is not None:
        if pair[0] > maxval or pair[1] > maxval:
            raise config.error(
                "Option '%s' in section bed_mesh must have a maximum of %s"
                % (option, str(maxval))
            )
    return pair


# retrieve comma separated pair from a g-code command
def parse_gcmd_pair(gcmd, name, minval=None, maxval=None):
    try:
        pair = [int(v.strip()) for v in gcmd.get(name).split(",")]
    except:
        raise gcmd.error("Unable to parse parameter '%s'" % (name,))
    if len(pair) != 2:
        if len(pair) != 1:
            raise gcmd.error("Unable to parse parameter '%s'" % (name,))
        pair = (pair[0], pair[0])
    if minval is not None:
        if pair[0] < minval or pair[1] < minval:
            raise gcmd.error(
                "Parameter '%s' must have a minimum of %d" % (name, minval)
            )
    if maxval is not None:
        if pair[0] > maxval or pair[1] > maxval:
            raise gcmd.error(
                "Parameter '%s' must have a maximum of %d" % (name, maxval)
            )
    return pair


# retrieve comma separated coordinate from a g-code command
def parse_gcmd_coord(gcmd, name):
    try:
        v1, v2 = [float(v.strip()) for v in gcmd.get(name).split(",")]
    except:
        raise gcmd.error("Unable to parse parameter '%s'" % (name,))
    return v1, v2


class BedMesh:
    FADE_DISABLE = 0x7FFFFFFF

    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.last_position = [0.0, 0.0, 0.0, 0.0]
        self.bmc = BedMeshCalibrate(config, self)
        self.z_mesh = None
        self.toolhead = None
        self.horizontal_move_z = config.getfloat("horizontal_move_z", 5.0)
        self.fade_start = config.getfloat("fade_start", 1.0)
        self.fade_end = config.getfloat("fade_end", 0.0)
        self.fade_dist = self.fade_end - self.fade_start
        if self.fade_dist <= 0.0:
            self.fade_start = self.fade_end = self.FADE_DISABLE
        self.log_fade_complete = False
        self.base_fade_target = config.getfloat("fade_target", None)
        self.fade_target = 0.0
        self.tool_offset = 0.0
        self.gcode = self.printer.lookup_object("gcode")
        self.splitter = MoveSplitter(config, self.gcode)
        # setup persistent storage
        self.pmgr = ProfileManager(config, self)
        self.save_profile = self.pmgr.save_profile
        # register gcodes
        self.gcode.register_command(
            "BED_MESH_OUTPUT",
            self.cmd_BED_MESH_OUTPUT,
            desc=self.cmd_BED_MESH_OUTPUT_help,
        )
        self.gcode.register_command(
            "BED_MESH_MAP", self.cmd_BED_MESH_MAP, desc=self.cmd_BED_MESH_MAP_help
        )
        self.gcode.register_command(
            "BED_MESH_CLEAR", self.cmd_BED_MESH_CLEAR, desc=self.cmd_BED_MESH_CLEAR_help
        )
        self.gcode.register_command(
            "BED_MESH_OFFSET",
            self.cmd_BED_MESH_OFFSET,
            desc=self.cmd_BED_MESH_OFFSET_help,
        )
        # Register dump webhooks
        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint("bed_mesh/dump_mesh", self._handle_dump_request)
        # Register transform
        gcode_move = self.printer.load_object(config, "gcode_move")
        gcode_move.set_move_transform(self)
        # initialize status dict
        self.update_status()

    def handle_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.bmc.print_generated_points(logging.info, truncate=True)

    def set_mesh(self, mesh):
        if mesh is not None and self.fade_end != self.FADE_DISABLE:
            self.log_fade_complete = True
            if self.base_fade_target is None:
                self.fade_target = mesh.get_z_average()
            else:
                self.fade_target = self.base_fade_target
                min_z, max_z = mesh.get_z_range()
                if not min_z <= self.fade_target <= max_z and self.fade_target != 0.0:
                    # fade target is non-zero, out of mesh range
                    err_target = self.fade_target
                    self.z_mesh = None
                    self.fade_target = 0.0
                    raise self.gcode.error(
                        "bed_mesh: ERROR, fade_target lies outside of mesh z "
                        "range\nmin: %.4f, max: %.4f, fade_target: %.4f"
                        % (min_z, max_z, err_target)
                    )
            min_z, max_z = mesh.get_z_range()
            if self.fade_dist <= max(abs(min_z), abs(max_z)):
                self.z_mesh = None
                self.fade_target = 0.0
                raise self.gcode.error(
                    "bed_mesh:  Mesh extends outside of the fade range, "
                    "please see the fade_start and fade_end options in"
                    "example-extras.cfg. fade distance: %.2f mesh min: %.4f"
                    "mesh max: %.4f" % (self.fade_dist, min_z, max_z)
                )
        else:
            self.fade_target = 0.0
        self.tool_offset = 0.0
        self.z_mesh = mesh
        self.splitter.initialize(mesh, self.fade_target)
        # cache the current position before a transform takes place
        gcode_move = self.printer.lookup_object("gcode_move")
        gcode_move.reset_last_position()
        self.update_status()

    def get_z_factor(self, z_pos):
        z_pos += self.tool_offset
        if z_pos >= self.fade_end:
            return 0.0
        elif z_pos >= self.fade_start:
            return (self.fade_end - z_pos) / self.fade_dist
        else:
            return 1.0

    def get_position(self):
        # Return last, non-transformed position
        if self.z_mesh is None:
            # No mesh calibrated, so send toolhead position
            self.last_position[:] = self.toolhead.get_position()
            self.last_position[2] -= self.fade_target
        else:
            # return current position minus the current z-adjustment
            cur_pos = self.toolhead.get_position()
            x, y, z = cur_pos[:3]
            max_adj = self.z_mesh.calc_z(x, y)
            factor = 1.0
            z_adj = max_adj - self.fade_target
            fade_z_pos = z + self.tool_offset
            if min(fade_z_pos, (fade_z_pos - max_adj)) >= self.fade_end:
                # Fade out is complete, no factor
                factor = 0.0
            elif max(fade_z_pos, (fade_z_pos - max_adj)) >= self.fade_start:
                # Likely in the process of fading out adjustment.
                # Because we don't yet know the gcode z position, use
                # algebra to calculate the factor from the toolhead pos
                factor = (self.fade_end + self.fade_target - fade_z_pos) / (
                    self.fade_dist - z_adj
                )
                factor = constrain(factor, 0.0, 1.0)
            final_z_adj = factor * z_adj + self.fade_target
            self.last_position[:] = [x, y, z - final_z_adj] + cur_pos[3:]
        return list(self.last_position)

    def move(self, newpos, speed):
        factor = self.get_z_factor(newpos[2])
        if self.z_mesh is None or not factor:
            # No mesh calibrated, or mesh leveling phased out.
            x, y, z = newpos[:3]
            if self.log_fade_complete:
                self.log_fade_complete = False
                logging.info(
                    "bed_mesh fade complete: Current Z: %.4f fade_target: %.4f "
                    % (z, self.fade_target)
                )
            self.toolhead.move([x, y, z + self.fade_target] + newpos[3:], speed)
        else:
            self.splitter.build_move(self.last_position, newpos, factor)
            while not self.splitter.traverse_complete:
                split_move = self.splitter.split()
                if split_move:
                    self.toolhead.move(split_move, speed)
                else:
                    raise self.gcode.error("Mesh Leveling: Error splitting move ")
        self.last_position[:] = newpos

    def get_status(self, eventtime=None):
        return self.status

    def update_status(self):
        self.status = {
            "profile_name": "",
            "mesh_min": (0.0, 0.0),
            "mesh_max": (0.0, 0.0),
            "probed_matrix": [[]],
            "mesh_matrix": [[]],
            "profiles": self.pmgr.get_profiles(),
        }
        if self.z_mesh is not None:
            params = self.z_mesh.get_mesh_params()
            mesh_min = (params["min_x"], params["min_y"])
            mesh_max = (params["max_x"], params["max_y"])
            probed_matrix = self.z_mesh.get_probed_matrix()
            mesh_matrix = self.z_mesh.get_mesh_matrix()
            self.status["profile_name"] = self.z_mesh.get_profile_name()
            self.status["mesh_min"] = mesh_min
            self.status["mesh_max"] = mesh_max
            self.status["probed_matrix"] = probed_matrix
            self.status["mesh_matrix"] = mesh_matrix

    def get_mesh(self):
        return self.z_mesh

    cmd_BED_MESH_OUTPUT_help = "Retrieve interpolated grid of probed z-points"

    def cmd_BED_MESH_OUTPUT(self, gcmd):
        if gcmd.get_int("PGP", 0):
            # Print Generated Points instead of mesh
            self.bmc.print_generated_points(gcmd.respond_info)
        elif self.z_mesh is None:
            gcmd.respond_info("Bed has not been probed")
        else:
            self.z_mesh.print_probed_matrix(gcmd.respond_info)
            self.z_mesh.print_mesh(gcmd.respond_raw, self.horizontal_move_z)

    cmd_BED_MESH_MAP_help = "Serialize mesh and output to terminal"

    def cmd_BED_MESH_MAP(self, gcmd):
        if self.z_mesh is not None:
            params = self.z_mesh.get_mesh_params()
            outdict = {
                "mesh_min": (params["min_x"], params["min_y"]),
                "mesh_max": (params["max_x"], params["max_y"]),
                "z_positions": self.z_mesh.get_probed_matrix(),
            }
            gcmd.respond_raw("mesh_map_output " + json.dumps(outdict))
        else:
            gcmd.respond_info("Bed has not been probed")

    cmd_BED_MESH_CLEAR_help = "Clear the Mesh so no z-adjustment is made"

    def cmd_BED_MESH_CLEAR(self, gcmd):
        self.set_mesh(None)

    cmd_BED_MESH_OFFSET_help = "Add X/Y offsets to the mesh lookup"

    def cmd_BED_MESH_OFFSET(self, gcmd):
        if self.z_mesh is not None:
            offsets = [None, None]
            for i, axis in enumerate(["X", "Y"]):
                offsets[i] = gcmd.get_float(axis, None)
            self.z_mesh.set_mesh_offsets(offsets)
            tool_offset = gcmd.get_float("ZFADE", None)
            if tool_offset is not None:
                self.tool_offset = tool_offset
            gcode_move = self.printer.lookup_object("gcode_move")
            gcode_move.reset_last_position()
        else:
            gcmd.respond_info("No mesh loaded to offset")

    def _handle_dump_request(self, web_request):
        eventtime = self.printer.get_reactor().monotonic()
        prb = self.printer.lookup_object("probe", None)
        th_sts = self.printer.lookup_object("toolhead").get_status(eventtime)
        result = {"current_mesh": {}, "profiles": self.pmgr.get_profiles()}
        if self.z_mesh is not None:
            result["current_mesh"] = {
                "name": self.z_mesh.get_profile_name(),
                "probed_matrix": self.z_mesh.get_probed_matrix(),
                "mesh_matrix": self.z_mesh.get_mesh_matrix(),
                "mesh_params": self.z_mesh.get_mesh_params(),
            }
        mesh_args = web_request.get_dict("mesh_args", {})
        gcmd = None
        if mesh_args:
            gcmd = self.gcode.create_gcode_command("", "", mesh_args)
            with self.gcode.get_mutex():
                result["calibration"] = self.bmc.dump_calibration(gcmd)
        else:
            result["calibration"] = self.bmc.dump_calibration()
        offsets = [0, 0, 0] if prb is None else prb.get_offsets()
        result["probe_offsets"] = offsets
        result["axis_minimum"] = th_sts["axis_minimum"]
        result["axis_maximum"] = th_sts["axis_maximum"]
        web_request.send(result)


class ZrefMode:
    DISABLED = 0  # Zero reference disabled
    IN_MESH = 1  # Zero reference position within mesh
    PROBE = 2  # Zero refrennce position outside of mesh, probe needed


class BedMeshCalibrate:
    ALGOS = ["lagrange", "bicubic"]

    def __init__(self, config, bedmesh):
        self.printer = config.get_printer()
        self.orig_config = {"radius": None, "origin": None}
        self.radius = self.origin = None
        self.mesh_min = self.mesh_max = (0.0, 0.0)
        self.adaptive_margin = config.getfloat("adaptive_margin", 0.0)
        self.bedmesh = bedmesh
        self.mesh_config = collections.OrderedDict()
        self._init_mesh_config(config)
        self.probe_mgr = ProbeManager(config, self.orig_config, self.probe_finalize)
        try:
            self.probe_mgr.generate_points(
                self.mesh_config, self.mesh_min, self.mesh_max, self.radius, self.origin
            )
        except BedMeshError as e:
            raise config.error(str(e))
        self._profile_name = "default"
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "BED_MESH_CALIBRATE",
            self.cmd_BED_MESH_CALIBRATE,
            desc=self.cmd_BED_MESH_CALIBRATE_help,
        )

    def print_generated_points(self, print_func, truncate=False):
        x_offset = y_offset = 0.0
        probe = self.printer.lookup_object("probe", None)
        if probe is not None:
            x_offset, y_offset = probe.get_offsets()[:2]
        print_func("bed_mesh: generated points\nIndex |  Tool Adjusted  |   Probe")
        points = self.probe_mgr.get_base_points()
        for i, (x, y) in enumerate(points):
            if i >= 50 and truncate:
                end = len(points) - 1
                print_func("...points %d through %d truncated" % (i, end))
                break
            adj_pt = "(%.1f, %.1f)" % (x - x_offset, y - y_offset)
            mesh_pt = "(%.1f, %.1f)" % (x, y)
            print_func("  %-4d| %-16s| %s" % (i, adj_pt, mesh_pt))
        zero_ref_pos = self.probe_mgr.get_zero_ref_pos()
        if zero_ref_pos is not None:
            print_func(
                "bed_mesh: zero_reference_position is (%.2f, %.2f)"
                % (zero_ref_pos[0], zero_ref_pos[1])
            )
        substitutes = self.probe_mgr.get_substitutes()
        if substitutes:
            print_func("bed_mesh: faulty region points")
            for i, v in substitutes.items():
                pt = points[i]
                print_func(
                    "%d (%.2f, %.2f), substituted points: %s"
                    % (i, pt[0], pt[1], repr(v))
                )

    def _init_mesh_config(self, config):
        mesh_cfg = self.mesh_config
        orig_cfg = self.orig_config
        self.radius = config.getfloat("mesh_radius", None, above=0.0)
        if self.radius is not None:
            self.origin = config.getfloatlist("mesh_origin", (0.0, 0.0), count=2)
            x_cnt = y_cnt = config.getint("round_probe_count", 5, minval=3)
            # round beds must have an odd number of points along each axis
            if not x_cnt & 1:
                raise config.error("bed_mesh: probe_count must be odd for round beds")
            # radius may have precision to .1mm
            self.radius = math.floor(self.radius * 10) / 10
            orig_cfg["radius"] = self.radius
            orig_cfg["origin"] = self.origin
            min_x = min_y = -self.radius
            max_x = max_y = self.radius
        else:
            # rectangular
            x_cnt, y_cnt = parse_config_pair(config, "probe_count", 3, minval=3)
            min_x, min_y = config.getfloatlist("mesh_min", count=2)
            max_x, max_y = config.getfloatlist("mesh_max", count=2)
            if max_x <= min_x or max_y <= min_y:
                raise config.error("bed_mesh: invalid min/max points")
        orig_cfg["x_count"] = mesh_cfg["x_count"] = x_cnt
        orig_cfg["y_count"] = mesh_cfg["y_count"] = y_cnt
        orig_cfg["mesh_min"] = self.mesh_min = (min_x, min_y)
        orig_cfg["mesh_max"] = self.mesh_max = (max_x, max_y)

        pps = parse_config_pair(config, "mesh_pps", 2, minval=0)
        orig_cfg["mesh_x_pps"] = mesh_cfg["mesh_x_pps"] = pps[0]
        orig_cfg["mesh_y_pps"] = mesh_cfg["mesh_y_pps"] = pps[1]
        orig_cfg["algo"] = mesh_cfg["algo"] = (
            config.get("algorithm", "lagrange").strip().lower()
        )
        orig_cfg["tension"] = mesh_cfg["tension"] = config.getfloat(
            "bicubic_tension", 0.2, minval=0.0, maxval=2.0
        )
        self._verify_algorithm(config.error)

    def _verify_algorithm(self, error):
        params = self.mesh_config
        x_pps = params["mesh_x_pps"]
        y_pps = params["mesh_y_pps"]
        if params["algo"] not in self.ALGOS:
            raise error("bed_mesh: Unknown algorithm <%s>" % (self.mesh_config["algo"]))
        # Check the algorithm against the current configuration
        max_probe_cnt = max(params["x_count"], params["y_count"])
        min_probe_cnt = min(params["x_count"], params["y_count"])
        if max(x_pps, y_pps) == 0:
            # Interpolation disabled
            self.mesh_config["algo"] = "direct"
        elif params["algo"] == "lagrange" and max_probe_cnt > 6:
            # Lagrange interpolation tends to oscillate when using more
            # than 6 samples
            raise error(
                "bed_mesh: cannot exceed a probe_count of 6 when using "
                "lagrange interpolation. Configured Probe Count: %d, %d"
                % (self.mesh_config["x_count"], self.mesh_config["y_count"])
            )
        elif params["algo"] == "bicubic" and min_probe_cnt < 4:
            if max_probe_cnt > 6:
                raise error(
                    "bed_mesh: invalid probe_count option when using bicubic "
                    "interpolation.  Combination of 3 points on one axis with "
                    "more than 6 on another is not permitted. "
                    "Configured Probe Count: %d, %d"
                    % (self.mesh_config["x_count"], self.mesh_config["y_count"])
                )
            else:
                logging.info(
                    "bed_mesh: bicubic interpolation with a probe_count of "
                    "less than 4 points detected.  Forcing lagrange "
                    "interpolation. Configured Probe Count: %d, %d"
                    % (self.mesh_config["x_count"], self.mesh_config["y_count"])
                )
                params["algo"] = "lagrange"

    def set_adaptive_mesh(self, gcmd):
        if not gcmd.get_int("ADAPTIVE", 0):
            return False
        exclude_objects = self.printer.lookup_object("exclude_object", None)
        if exclude_objects is None:
            gcmd.respond_info("Exclude objects not enabled. Using full mesh...")
            return False
        objects = exclude_objects.get_status().get("objects", [])
        if not objects:
            return False
        margin = gcmd.get_float("ADAPTIVE_MARGIN", self.adaptive_margin)

        # List all exclude_object points by axis and iterate over
        # all polygon points, and pick the min and max or each axis
        list_of_xs = []
        list_of_ys = []
        gcmd.respond_info("Found %s objects" % (len(objects)))
        for obj in objects:
            for point in obj["polygon"]:
                list_of_xs.append(point[0])
                list_of_ys.append(point[1])

        # Define bounds of adaptive mesh area
        mesh_min = [min(list_of_xs), min(list_of_ys)]
        mesh_max = [max(list_of_xs), max(list_of_ys)]
        adjusted_mesh_min = [x - margin for x in mesh_min]
        adjusted_mesh_max = [x + margin for x in mesh_max]

        # Force margin to respect original mesh bounds
        adjusted_mesh_min[0] = max(
            adjusted_mesh_min[0], self.orig_config["mesh_min"][0]
        )
        adjusted_mesh_min[1] = max(
            adjusted_mesh_min[1], self.orig_config["mesh_min"][1]
        )
        adjusted_mesh_max[0] = min(
            adjusted_mesh_max[0], self.orig_config["mesh_max"][0]
        )
        adjusted_mesh_max[1] = min(
            adjusted_mesh_max[1], self.orig_config["mesh_max"][1]
        )

        adjusted_mesh_size = (
            adjusted_mesh_max[0] - adjusted_mesh_min[0],
            adjusted_mesh_max[1] - adjusted_mesh_min[1],
        )

        # Compute a ratio between the adapted and original sizes
        ratio = (
            adjusted_mesh_size[0]
            / (self.orig_config["mesh_max"][0] - self.orig_config["mesh_min"][0]),
            adjusted_mesh_size[1]
            / (self.orig_config["mesh_max"][1] - self.orig_config["mesh_min"][1]),
        )

        gcmd.respond_info(
            "Original mesh bounds: (%s,%s)"
            % (self.orig_config["mesh_min"], self.orig_config["mesh_max"])
        )
        gcmd.respond_info(
            "Original probe count: (%s,%s)"
            % (self.mesh_config["x_count"], self.mesh_config["y_count"])
        )
        gcmd.respond_info(
            "Adapted mesh bounds: (%s,%s)" % (adjusted_mesh_min, adjusted_mesh_max)
        )
        gcmd.respond_info("Ratio: (%s, %s)" % ratio)

        new_x_probe_count = int(math.ceil(self.mesh_config["x_count"] * ratio[0]))
        new_y_probe_count = int(math.ceil(self.mesh_config["y_count"] * ratio[1]))

        # There is one case, where we may have to adjust the probe counts:
        # axis0 < 4 and axis1 > 6 (see _verify_algorithm).
        min_num_of_probes = 3
        if (
            max(new_x_probe_count, new_y_probe_count) > 6
            and min(new_x_probe_count, new_y_probe_count) < 4
        ):
            min_num_of_probes = 4

        new_x_probe_count = max(min_num_of_probes, new_x_probe_count)
        new_y_probe_count = max(min_num_of_probes, new_y_probe_count)

        gcmd.respond_info(
            "Adapted probe count: (%s,%s)" % (new_x_probe_count, new_y_probe_count)
        )

        # If the adapted mesh size is too small, adjust it to something
        # useful.
        adjusted_mesh_size = (
            max(adjusted_mesh_size[0], new_x_probe_count),
            max(adjusted_mesh_size[1], new_y_probe_count),
        )

        if self.radius is not None:
            adapted_radius = (
                math.sqrt((adjusted_mesh_size[0] ** 2) + (adjusted_mesh_size[1] ** 2))
                / 2
            )
            adapted_origin = (
                adjusted_mesh_min[0] + (adjusted_mesh_size[0] / 2),
                adjusted_mesh_min[1] + (adjusted_mesh_size[1] / 2),
            )
            to_adapted_origin = math.sqrt(
                adapted_origin[0] ** 2 + adapted_origin[1] ** 2
            )
            # If the adapted mesh size is smaller than the default/full
            # mesh, adjust the parameters. Otherwise, just do the full mesh.
            if adapted_radius + to_adapted_origin < self.radius:
                self.radius = adapted_radius
                self.origin = adapted_origin
                self.mesh_min = (-self.radius, -self.radius)
                self.mesh_max = (self.radius, self.radius)
                new_probe_count = max(new_x_probe_count, new_y_probe_count)
                # Adaptive meshes require odd number of points
                new_probe_count += 1 - (new_probe_count % 2)
                self.mesh_config["x_count"] = self.mesh_config["y_count"] = (
                    new_probe_count
                )
        else:
            self.mesh_min = adjusted_mesh_min
            self.mesh_max = adjusted_mesh_max
            self.mesh_config["x_count"] = new_x_probe_count
            self.mesh_config["y_count"] = new_y_probe_count
        self._profile_name = None
        return True

    def update_config(self, gcmd):
        # reset default configuration
        self.radius = self.orig_config["radius"]
        self.origin = self.orig_config["origin"]
        self.mesh_min = self.orig_config["mesh_min"]
        self.mesh_max = self.orig_config["mesh_max"]
        for key in list(self.mesh_config.keys()):
            self.mesh_config[key] = self.orig_config[key]

        params = gcmd.get_command_parameters()
        need_cfg_update = False
        if self.radius is not None:
            if "MESH_RADIUS" in params:
                self.radius = gcmd.get_float("MESH_RADIUS")
                self.radius = math.floor(self.radius * 10) / 10
                self.mesh_min = (-self.radius, -self.radius)
                self.mesh_max = (self.radius, self.radius)
                need_cfg_update = True
            if "MESH_ORIGIN" in params:
                self.origin = parse_gcmd_coord(gcmd, "MESH_ORIGIN")
                need_cfg_update = True
            if "ROUND_PROBE_COUNT" in params:
                cnt = gcmd.get_int("ROUND_PROBE_COUNT", minval=3)
                self.mesh_config["x_count"] = cnt
                self.mesh_config["y_count"] = cnt
                need_cfg_update = True
        else:
            if "MESH_MIN" in params:
                self.mesh_min = parse_gcmd_coord(gcmd, "MESH_MIN")
                need_cfg_update = True
            if "MESH_MAX" in params:
                self.mesh_max = parse_gcmd_coord(gcmd, "MESH_MAX")
                need_cfg_update = True
            if "PROBE_COUNT" in params:
                x_cnt, y_cnt = parse_gcmd_pair(gcmd, "PROBE_COUNT", minval=3)
                self.mesh_config["x_count"] = x_cnt
                self.mesh_config["y_count"] = y_cnt
                need_cfg_update = True

        if "MESH_PPS" in params:
            xpps, ypps = parse_gcmd_pair(gcmd, "MESH_PPS", minval=0)
            self.mesh_config["mesh_x_pps"] = xpps
            self.mesh_config["mesh_y_pps"] = ypps
            need_cfg_update = True

        if "ALGORITHM" in params:
            self.mesh_config["algo"] = gcmd.get("ALGORITHM").strip().lower()
            need_cfg_update = True

        need_cfg_update |= self.set_adaptive_mesh(gcmd)
        probe_method = gcmd.get("METHOD", "automatic")

        if need_cfg_update:
            self._verify_algorithm(gcmd.error)
            self.probe_mgr.generate_points(
                self.mesh_config,
                self.mesh_min,
                self.mesh_max,
                self.radius,
                self.origin,
                probe_method,
            )
            msg = "\n".join(["%s: %s" % (k, v) for k, v in self.mesh_config.items()])
            logging.info("Updated Mesh Configuration:\n" + msg)
        else:
            self.probe_mgr.generate_points(
                self.mesh_config,
                self.mesh_min,
                self.mesh_max,
                self.radius,
                self.origin,
                probe_method,
            )

    def dump_calibration(self, gcmd=None):
        if gcmd is not None and gcmd.get_command_parameters():
            self.update_config(gcmd)
        cfg = dict(self.mesh_config)
        cfg["mesh_min"] = self.mesh_min
        cfg["mesh_max"] = self.mesh_max
        cfg["origin"] = self.origin
        cfg["radius"] = self.radius
        return {
            "points": self.probe_mgr.get_base_points(),
            "config": cfg,
            "probe_path": self.probe_mgr.get_std_path(),
            "rapid_path": list(self.probe_mgr.iter_rapid_path()),
        }

    cmd_BED_MESH_CALIBRATE_help = "Perform Mesh Bed Leveling"

    def cmd_BED_MESH_CALIBRATE(self, gcmd):
        self._profile_name = gcmd.get("PROFILE", "default")
        if not self._profile_name.strip():
            raise gcmd.error("Value for parameter 'PROFILE' must be specified")
        self.bedmesh.set_mesh(None)
        try:
            self.update_config(gcmd)
        except BedMeshError as e:
            raise gcmd.error(str(e))
        self.probe_mgr.start_probe(gcmd)

    def probe_finalize(self, offsets, positions):
        z_offset = offsets[2]
        positions = [[round(p[0], 2), round(p[1], 2), p[2]] for p in positions]
        if self.probe_mgr.get_zero_ref_mode() == ZrefMode.PROBE:
            ref_pos = positions.pop()
            logging.info(
                "bed_mesh: z-offset replaced with probed z value at "
                "position (%.2f, %.2f, %.6f)" % (ref_pos[0], ref_pos[1], ref_pos[2])
            )
            z_offset = ref_pos[2]
        base_points = self.probe_mgr.get_base_points()
        params = dict(self.mesh_config)
        params["min_x"] = min(base_points, key=lambda p: p[0])[0]
        params["max_x"] = max(base_points, key=lambda p: p[0])[0]
        params["min_y"] = min(base_points, key=lambda p: p[1])[1]
        params["max_y"] = max(base_points, key=lambda p: p[1])[1]
        x_cnt = params["x_count"]
        y_cnt = params["y_count"]

        substitutes = self.probe_mgr.get_substitutes()
        probed_pts = positions
        if substitutes:
            # Replace substituted points with the original generated
            # point.  Its Z Value is the average probed Z of the
            # substituted points.
            corrected_pts = []
            idx_offset = 0
            start_idx = 0
            for i, pts in substitutes.items():
                fpt = [p - o for p, o in zip(base_points[i], offsets[:2])]
                # offset the index to account for additional samples
                idx = i + idx_offset
                # Add "normal" points
                corrected_pts.extend(positions[start_idx:idx])
                avg_z = sum([p[2] for p in positions[idx : idx + len(pts)]]) / len(pts)
                idx_offset += len(pts) - 1
                start_idx = idx + len(pts)
                fpt.append(avg_z)
                logging.info(
                    "bed_mesh: Replacing value at faulty index %d"
                    " (%.4f, %.4f): avg value = %.6f, avg w/ z_offset = %.6f"
                    % (i, fpt[0], fpt[1], avg_z, avg_z - z_offset)
                )
                corrected_pts.append(fpt)
            corrected_pts.extend(positions[start_idx:])
            positions = corrected_pts

        # validate length of result
        if len(base_points) != len(positions):
            self._dump_points(probed_pts, positions, offsets)
            raise self.gcode.error(
                "bed_mesh: invalid position list size, "
                "generated count: %d, probed count: %d"
                % (len(base_points), len(positions))
            )

        probed_matrix = []
        row = []
        prev_pos = base_points[0]
        for pos, result in zip(base_points, positions):
            offset_pos = [p - o for p, o in zip(pos, offsets[:2])]
            if not isclose(offset_pos[0], result[0], abs_tol=0.5) or not isclose(
                offset_pos[1], result[1], abs_tol=0.5
            ):
                logging.info(
                    "bed_mesh: point deviation > .5mm: orig pt = (%.2f, %.2f)"
                    ", probed pt = (%.2f, %.2f)"
                    % (offset_pos[0], offset_pos[1], result[0], result[1])
                )
            z_pos = result[2] - z_offset
            if not isclose(pos[1], prev_pos[1], abs_tol=0.1):
                # y has changed, append row and start new
                probed_matrix.append(row)
                row = []
            if pos[0] > prev_pos[0]:
                # probed in the positive direction
                row.append(z_pos)
            else:
                # probed in the negative direction
                row.insert(0, z_pos)
            prev_pos = pos
        # append last row
        probed_matrix.append(row)

        # make sure the y-axis is the correct length
        if len(probed_matrix) != y_cnt:
            raise self.gcode.error(
                (
                    "bed_mesh: Invalid y-axis table length\n"
                    "Probed table length: %d Probed Table:\n%s"
                )
                % (len(probed_matrix), str(probed_matrix))
            )

        if self.radius is not None:
            # round bed, extrapolate probed values to create a square mesh
            for row in probed_matrix:
                row_size = len(row)
                if not row_size & 1:
                    # an even number of points in a row shouldn't be possible
                    msg = "bed_mesh: incorrect number of points sampled on X\n"
                    msg += "Probed Table:\n"
                    msg += str(probed_matrix)
                    raise self.gcode.error(msg)
                buf_cnt = (x_cnt - row_size) // 2
                if buf_cnt == 0:
                    continue
                left_buffer = [row[0]] * buf_cnt
                right_buffer = [row[row_size - 1]] * buf_cnt
                row[0:0] = left_buffer
                row.extend(right_buffer)

        #  make sure that the x-axis is the correct length
        for row in probed_matrix:
            if len(row) != x_cnt:
                raise self.gcode.error(
                    (
                        "bed_mesh: invalid x-axis table length\n"
                        "Probed table length: %d Probed Table:\n%s"
                    )
                    % (len(probed_matrix), str(probed_matrix))
                )

        z_mesh = ZMesh(params, self._profile_name)
        try:
            z_mesh.build_mesh(probed_matrix)
        except BedMeshError as e:
            raise self.gcode.error(str(e))
        if self.probe_mgr.get_zero_ref_mode() == ZrefMode.IN_MESH:
            # The reference can be anywhere in the mesh, therefore
            # it is necessary to set the reference after the initial mesh
            # is generated to lookup the correct z value.
            zero_ref_pos = self.probe_mgr.get_zero_ref_pos()
            z_mesh.set_zero_reference(*zero_ref_pos)
        self.bedmesh.set_mesh(z_mesh)
        self.gcode.respond_info("Mesh Bed Leveling Complete")
        if self._profile_name is not None:
            self.bedmesh.save_profile(self._profile_name)

    def _dump_points(self, probed_pts, corrected_pts, offsets):
        # logs generated points with offset applied, points received
        # from the finalize callback, and the list of corrected points
        points = self.probe_mgr.get_base_points()
        max_len = max([len(points), len(probed_pts), len(corrected_pts)])
        logging.info(
            "bed_mesh: calibration point dump\nIndex | %-17s| %-25s|"
            " Corrected Point" % ("Generated Point", "Probed Point")
        )
        for i in list(range(max_len)):
            gen_pt = probed_pt = corr_pt = ""
            if i < len(points):
                off_pt = [p - o for p, o in zip(points[i], offsets[:2])]
                gen_pt = "(%.2f, %.2f)" % tuple(off_pt)
            if i < len(probed_pts):
                probed_pt = "(%.2f, %.2f, %.4f)" % tuple(probed_pts[i])
            if i < len(corrected_pts):
                corr_pt = "(%.2f, %.2f, %.4f)" % tuple(corrected_pts[i])
            logging.info("  %-4d| %-17s| %-25s| %s" % (i, gen_pt, probed_pt, corr_pt))


class ProbeManager:
    def __init__(self, config, orig_config, finalize_cb):
        self.printer = config.get_printer()
        self.cfg_overshoot = config.getfloat("scan_overshoot", 0, minval=1.0)
        self.orig_config = orig_config
        self.faulty_regions = []
        self.overshoot = self.cfg_overshoot
        self.zero_ref_pos = config.getfloatlist(
            "zero_reference_position", None, count=2
        )
        self.zref_mode = ZrefMode.DISABLED
        self.base_points = []
        self.substitutes = collections.OrderedDict()
        self.is_round = orig_config["radius"] is not None
        self.probe_helper = probe.ProbePointsHelper(config, finalize_cb, [])
        self.probe_helper.use_xy_offsets(True)
        self.rapid_scan_helper = RapidScanHelper(config, self, finalize_cb)
        self._init_faulty_regions(config)

    def _init_faulty_regions(self, config):
        for i in list(range(1, 100, 1)):
            start = config.getfloatlist("faulty_region_%d_min" % (i,), None, count=2)
            if start is None:
                break
            end = config.getfloatlist("faulty_region_%d_max" % (i,), count=2)
            # Validate the corners.  If necessary reorganize them.
            # c1 = min point, c3 = max point
            #  c4 ---- c3
            #  |        |
            #  c1 ---- c2
            c1 = [min([s, e]) for s, e in zip(start, end)]
            c3 = [max([s, e]) for s, e in zip(start, end)]
            c2 = [c1[0], c3[1]]
            c4 = [c3[0], c1[1]]
            # Check for overlapping regions
            for j, (prev_c1, prev_c3) in enumerate(self.faulty_regions):
                prev_c2 = [prev_c1[0], prev_c3[1]]
                prev_c4 = [prev_c3[0], prev_c1[1]]
                # Validate that no existing corner is within the new region
                for coord in [prev_c1, prev_c2, prev_c3, prev_c4]:
                    if within(coord, c1, c3):
                        raise config.error(
                            "bed_mesh: Existing faulty_region_%d %s overlaps "
                            "added faulty_region_%d %s"
                            % (j + 1, repr([prev_c1, prev_c3]), i, repr([c1, c3]))
                        )
                # Validate that no new corner is within an existing region
                for coord in [c1, c2, c3, c4]:
                    if within(coord, prev_c1, prev_c3):
                        raise config.error(
                            "bed_mesh: Added faulty_region_%d %s overlaps "
                            "existing faulty_region_%d %s"
                            % (i, repr([c1, c3]), j + 1, repr([prev_c1, prev_c3]))
                        )
            self.faulty_regions.append((c1, c3))

    def start_probe(self, gcmd):
        method = gcmd.get("METHOD", "automatic").lower()
        can_scan = False
        pprobe = self.printer.lookup_object("probe", None)
        if pprobe is not None:
            probe_name = pprobe.get_status(None).get("name", "")
            can_scan = probe_name.startswith("probe_eddy_current")
        if method == "rapid_scan" and can_scan:
            self.rapid_scan_helper.perform_rapid_scan(gcmd)
        else:
            self.probe_helper.start_probe(gcmd)

    def get_zero_ref_pos(self):
        return self.zero_ref_pos

    def get_zero_ref_mode(self):
        return self.zref_mode

    def get_substitutes(self):
        return self.substitutes

    def generate_points(
        self, mesh_config, mesh_min, mesh_max, radius, origin, probe_method="automatic"
    ):
        x_cnt = mesh_config["x_count"]
        y_cnt = mesh_config["y_count"]
        min_x, min_y = mesh_min
        max_x, max_y = mesh_max
        x_dist = (max_x - min_x) / (x_cnt - 1)
        y_dist = (max_y - min_y) / (y_cnt - 1)
        # floor distances down to next hundredth
        x_dist = math.floor(x_dist * 100) / 100
        y_dist = math.floor(y_dist * 100) / 100
        if x_dist < 1.0 or y_dist < 1.0:
            raise BedMeshError("bed_mesh: min/max points too close together")

        if radius is not None:
            # round bed, min/max needs to be recalculated
            y_dist = x_dist
            new_r = (x_cnt // 2) * x_dist
            min_x = min_y = -new_r
            max_x = max_y = new_r
        else:
            # rectangular bed, only re-calc max_x
            max_x = min_x + x_dist * (x_cnt - 1)
        pos_y = min_y
        points = []
        for i in range(y_cnt):
            for j in range(x_cnt):
                if not i % 2:
                    # move in positive direction
                    pos_x = min_x + j * x_dist
                else:
                    # move in negative direction
                    pos_x = max_x - j * x_dist
                if radius is None:
                    # rectangular bed, append
                    points.append((pos_x, pos_y))
                else:
                    # round bed, check distance from origin
                    dist_from_origin = math.sqrt(pos_x * pos_x + pos_y * pos_y)
                    if dist_from_origin <= radius:
                        points.append((origin[0] + pos_x, origin[1] + pos_y))
            pos_y += y_dist
        if self.zero_ref_pos is None or probe_method == "manual":
            # Zero Reference Disabled
            self.zref_mode = ZrefMode.DISABLED
        elif within(self.zero_ref_pos, mesh_min, mesh_max):
            # Zero Reference position within mesh
            self.zref_mode = ZrefMode.IN_MESH
        else:
            # Zero Reference position outside of mesh
            self.zref_mode = ZrefMode.PROBE
        self.base_points = points
        self.substitutes.clear()
        # adjust overshoot
        og_min_x = self.orig_config["mesh_min"][0]
        og_max_x = self.orig_config["mesh_max"][0]
        add_ovs = min(max(0, min_x - og_min_x), max(0, og_max_x - max_x))
        self.overshoot = self.cfg_overshoot + math.floor(add_ovs)
        min_pt, max_pt = (min_x, min_y), (max_x, max_y)
        self._process_faulty_regions(min_pt, max_pt, radius)
        self.probe_helper.update_probe_points(self.get_std_path(), 3)

    def _process_faulty_regions(self, min_pt, max_pt, radius):
        if not self.faulty_regions:
            return
        # Cannot probe a reference within a faulty region
        if self.zref_mode == ZrefMode.PROBE:
            for min_c, max_c in self.faulty_regions:
                if within(self.zero_ref_pos, min_c, max_c):
                    opt = "zero_reference_position"
                    raise BedMeshError(
                        "bed_mesh: Cannot probe zero reference position at "
                        "(%.2f, %.2f) as it is located within a faulty region."
                        " Check the value for option '%s'"
                        % (
                            self.zero_ref_pos[0],
                            self.zero_ref_pos[1],
                            opt,
                        )
                    )
        # Check to see if any points fall within faulty regions
        last_y = self.base_points[0][1]
        is_reversed = False
        for i, coord in enumerate(self.base_points):
            if not isclose(coord[1], last_y):
                is_reversed = not is_reversed
            last_y = coord[1]
            adj_coords = []
            for min_c, max_c in self.faulty_regions:
                if within(coord, min_c, max_c, tol=0.00001):
                    # Point lies within a faulty region
                    adj_coords = [
                        (min_c[0], coord[1]),
                        (coord[0], min_c[1]),
                        (coord[0], max_c[1]),
                        (max_c[0], coord[1]),
                    ]
                    if is_reversed:
                        # Swap first and last points for zig-zag pattern
                        first = adj_coords[0]
                        adj_coords[0] = adj_coords[-1]
                        adj_coords[-1] = first
                    break
            if not adj_coords:
                # coord is not located within a faulty region
                continue
            valid_coords = []
            for ac in adj_coords:
                # make sure that coordinates are within the mesh boundary
                if radius is None:
                    if within(ac, min_pt, max_pt, 0.000001):
                        valid_coords.append(ac)
                else:
                    dist_from_origin = math.sqrt(ac[0] * ac[0] + ac[1] * ac[1])
                    if dist_from_origin <= radius:
                        valid_coords.append(ac)
            if not valid_coords:
                raise BedMeshError(
                    "bed_mesh: Unable to generate coordinates"
                    " for faulty region at index: %d" % (i)
                )
            self.substitutes[i] = valid_coords

    def get_base_points(self):
        return self.base_points

    def get_std_path(self):
        path = []
        for idx, pt in enumerate(self.base_points):
            if idx in self.substitutes:
                for sub_pt in self.substitutes[idx]:
                    path.append(sub_pt)
            else:
                path.append(pt)
        if self.zref_mode == ZrefMode.PROBE:
            path.append(self.zero_ref_pos)
        return path

    def iter_rapid_path(self):
        ascnd_x = True
        last_base_pt = last_mv_pt = self.base_points[0]
        # Generate initial move point
        if self.overshoot:
            overshoot = min(8, self.overshoot)
            last_mv_pt = (last_base_pt[0] - overshoot, last_base_pt[1])
            yield last_mv_pt, False
        for idx, pt in enumerate(self.base_points):
            # increasing Y indicates direction change
            dir_change = not isclose(pt[1], last_base_pt[1], abs_tol=1e-6)
            if idx in self.substitutes:
                fp_gen = self._gen_faulty_path(last_mv_pt, idx, ascnd_x, dir_change)
                for sub_pt, is_smp in fp_gen:
                    yield sub_pt, is_smp
                    last_mv_pt = sub_pt
            else:
                if dir_change:
                    for dpt in self._gen_dir_change(last_mv_pt, pt, ascnd_x):
                        yield dpt, False
                yield pt, True
                last_mv_pt = pt
            last_base_pt = pt
            ascnd_x ^= dir_change
        if self.zref_mode == ZrefMode.PROBE:
            if self.overshoot:
                ovs = min(4, self.overshoot)
                ovs = ovs if ascnd_x else -ovs
                yield (last_mv_pt[0] + ovs, last_mv_pt[1]), False
            yield self.zero_ref_pos, True

    def _gen_faulty_path(self, last_pt, idx, ascnd_x, dir_change):
        subs = self.substitutes[idx]
        sub_cnt = len(subs)
        if dir_change:
            for dpt in self._gen_dir_change(last_pt, subs[0], ascnd_x):
                yield dpt, False
        if self.is_round:
            # No faulty region path handling for round beds
            for pt in subs:
                yield pt, True
            return
        # Check to see if this is the first corner
        first_corner = False
        sorted_sub_idx = sorted(self.substitutes.keys())
        if sub_cnt == 2 and idx < len(sorted_sub_idx):
            first_corner = sorted_sub_idx[idx] == idx
        yield subs[0], True
        if sub_cnt == 1:
            return
        last_pt, next_pt = subs[:2]
        if sub_cnt == 2:
            if first_corner or dir_change:
                # horizontal move first
                yield (next_pt[0], last_pt[1]), False
            else:
                yield (last_pt[0], next_pt[1]), False
            yield next_pt, True
        elif sub_cnt >= 3:
            if dir_change:
                # first move should be a vertical switch up.  If overshoot
                # is available, simulate another direction change.  Otherwise
                # move inward 2 mm, then up through the faulty region.
                if self.overshoot:
                    for dpt in self._gen_dir_change(last_pt, next_pt, ascnd_x):
                        yield dpt, False
                else:
                    shift = -2 if ascnd_x else 2
                    yield (last_pt[0] + shift, last_pt[1]), False
                    yield (last_pt[0] + shift, next_pt[1]), False
                yield next_pt, True
                last_pt, next_pt = subs[1:3]
            else:
                # vertical move
                yield (last_pt[0], next_pt[1]), False
                yield next_pt, True
                last_pt, next_pt = subs[1:3]
                if sub_cnt == 4:
                    # Vertical switch up within faulty region
                    shift = 2 if ascnd_x else -2
                    yield (last_pt[0] + shift, last_pt[1]), False
                    yield (next_pt[0] - shift, next_pt[1]), False
                    yield next_pt, True
                    last_pt, next_pt = subs[2:4]
            # horizontal move before final point
            yield (next_pt[0], last_pt[1]), False
            yield next_pt, True

    def _gen_dir_change(self, last_pt, next_pt, ascnd_x):
        if not self.overshoot:
            return
        # overshoot X beyond the outer point
        xdir = 1 if ascnd_x else -1
        overshoot = 2.0 if self.overshoot >= 3.0 else self.overshoot
        ovr_pt = (last_pt[0] + overshoot * xdir, last_pt[1])
        yield ovr_pt
        if self.overshoot < 3.0:
            # No room to generate an arc, move up to next y
            yield (next_pt[0] + overshoot * xdir, next_pt[1])
        else:
            # generate arc
            STEP_ANGLE = 3
            START_ANGLE = 270
            ydiff = abs(next_pt[1] - last_pt[1])
            xdiff = abs(next_pt[0] - last_pt[0])
            max_radius = min(self.overshoot - 2, 8)
            radius = min(ydiff / 2, max_radius)
            origin = [ovr_pt[0], last_pt[1] + radius]
            next_origin_y = next_pt[1] - radius
            # determine angle
            if xdiff < 0.01:
                # Move is aligned on the x-axis
                angle = 90
                if next_origin_y - origin[1] < 0.05:
                    # The move can be completed in a single arc
                    angle = 180
            else:
                angle = int(math.degrees(math.atan(ydiff / xdiff)))
                if (ascnd_x and next_pt[0] < last_pt[0]) or (
                    not ascnd_x and next_pt[0] > last_pt[0]
                ):
                    angle = 180 - angle
            count = int(angle // STEP_ANGLE)
            # Gen first arc
            step = STEP_ANGLE * xdir
            start = START_ANGLE + step
            for arc_pt in self._gen_arc(origin, radius, start, step, count):
                yield arc_pt
            if angle == 180:
                # arc complete
                return
            # generate next arc
            origin = [next_pt[0] + overshoot * xdir, next_origin_y]
            # start at the angle where the last arc finished
            start = START_ANGLE + count * step
            # recalculate the count to make sure we generate a full 180
            # degrees.  Add a step for the repeated connecting angle
            count = 61 - count
            for arc_pt in self._gen_arc(origin, radius, start, step, count):
                yield arc_pt

    def _gen_arc(self, origin, radius, start, step, count):
        end = start + step * count
        # create a segent for every 3 degrees of travel
        for angle in range(start, end, step):
            rad = math.radians(angle % 360)
            opp = math.sin(rad) * radius
            adj = math.cos(rad) * radius
            yield (origin[0] + adj, origin[1] + opp)


MAX_HIT_DIST = 2.0
MM_WIN_SPEED = 125


class RapidScanHelper:
    def __init__(self, config, probe_mgr, finalize_cb):
        self.printer = config.get_printer()
        self.probe_manager = probe_mgr
        self.speed = config.getfloat("speed", 50.0, above=0.0)
        self.scan_height = config.getfloat("horizontal_move_z", 5.0)
        self.finalize_callback = finalize_cb

    def perform_rapid_scan(self, gcmd):
        speed = gcmd.get_float("SCAN_SPEED", self.speed)
        scan_height = gcmd.get_float("HORIZONTAL_MOVE_Z", self.scan_height)
        gcmd.respond_info(
            "Beginning rapid surface scan at height %.2f..." % (scan_height)
        )
        pprobe = self.printer.lookup_object("probe")
        toolhead = self.printer.lookup_object("toolhead")
        # Calculate time window around which a sample is valid.  Current
        # assumption is anything within 2mm is usable, so:
        # window = 2 / max_speed
        #
        # TODO: validate maximum speed allowed based on sample rate of probe
        # Scale the hit distance window for speeds lower than 125mm/s.  The
        # lower the speed the less the window shrinks.
        scale = max(0, 1 - speed / MM_WIN_SPEED) + 1
        hit_dist = min(MAX_HIT_DIST, scale * speed / MM_WIN_SPEED)
        half_window = hit_dist / speed
        gcmd.respond_info(
            "Sample hit distance +/- %.4fmm, time window +/- ms %.4f"
            % (hit_dist, half_window * 1000)
        )
        gcmd_params = gcmd.get_command_parameters()
        gcmd_params["SAMPLE_TIME"] = half_window * 2
        self._raise_tool(gcmd, scan_height)
        probe_session = pprobe.start_probe_session(gcmd)
        offsets = pprobe.get_offsets()
        initial_move = True
        for pos, is_probe_pt in self.probe_manager.iter_rapid_path():
            pos = self._apply_offsets(pos[:2], offsets)
            toolhead.manual_move(pos, speed)
            if initial_move:
                initial_move = False
                self._move_to_scan_height(gcmd, scan_height)
            if is_probe_pt:
                probe_session.run_probe(gcmd)
        results = probe_session.pull_probed_results()
        toolhead.get_last_move_time()
        self.finalize_callback(offsets, results)
        probe_session.end_probe_session()

    def _raise_tool(self, gcmd, scan_height):
        # If the nozzle is below scan height raise the tool
        toolhead = self.printer.lookup_object("toolhead")
        pprobe = self.printer.lookup_object("probe")
        cur_pos = toolhead.get_position()
        if cur_pos[2] >= scan_height:
            return
        pparams = pprobe.get_probe_params(gcmd)
        lift_speed = pparams["lift_speed"]
        cur_pos[2] = self.scan_height + 0.5
        toolhead.manual_move(cur_pos, lift_speed)

    def _move_to_scan_height(self, gcmd, scan_height):
        time_window = gcmd.get_float("SAMPLE_TIME")
        toolhead = self.printer.lookup_object("toolhead")
        pprobe = self.printer.lookup_object("probe")
        cur_pos = toolhead.get_position()
        pparams = pprobe.get_probe_params(gcmd)
        lift_speed = pparams["lift_speed"]
        probe_speed = pparams["probe_speed"]
        cur_pos[2] = scan_height + 0.5
        toolhead.manual_move(cur_pos, lift_speed)
        cur_pos[2] = scan_height
        toolhead.manual_move(cur_pos, probe_speed)
        toolhead.dwell(time_window / 2 + 0.01)

    def _apply_offsets(self, point, offsets):
        return [(pos - ofs) for pos, ofs in zip(point, offsets)]


class MoveSplitter:
    def __init__(self, config, gcode):
        self.split_delta_z = config.getfloat("split_delta_z", 0.025, minval=0.01)
        self.move_check_distance = config.getfloat(
            "move_check_distance", 5.0, minval=3.0
        )
        self.z_mesh = None
        self.fade_offset = 0.0
        self.gcode = gcode

    def initialize(self, mesh, fade_offset):
        self.z_mesh = mesh
        self.fade_offset = fade_offset

    def build_move(self, prev_pos, next_pos, factor):
        self.prev_pos = tuple(prev_pos)
        self.next_pos = tuple(next_pos)
        self.current_pos = list(prev_pos)
        self.z_factor = factor
        self.z_offset = self._calc_z_offset(prev_pos)
        self.traverse_complete = False
        self.distance_checked = 0.0
        axes_d = [np - pp for np, pp in zip(self.next_pos, self.prev_pos)]
        self.total_move_length = math.sqrt(sum([d * d for d in axes_d[:3]]))
        self.axis_move = [not isclose(d, 0.0, abs_tol=1e-10) for d in axes_d]

    def _calc_z_offset(self, pos):
        z = self.z_mesh.calc_z(pos[0], pos[1])
        offset = self.fade_offset
        return self.z_factor * (z - offset) + offset

    def _set_next_move(self, distance_from_prev):
        t = distance_from_prev / self.total_move_length
        if t > 1.0 or t < 0.0:
            raise self.gcode.error(
                "bed_mesh: Slice distance is negative "
                "or greater than entire move length"
            )
        for i in range(len(self.next_pos)):
            if self.axis_move[i]:
                self.current_pos[i] = lerp(t, self.prev_pos[i], self.next_pos[i])

    def split(self):
        if not self.traverse_complete:
            if self.axis_move[0] or self.axis_move[1]:
                # X and/or Y axis move, traverse if necessary
                while (
                    self.distance_checked + self.move_check_distance
                    < self.total_move_length
                ):
                    self.distance_checked += self.move_check_distance
                    self._set_next_move(self.distance_checked)
                    next_z = self._calc_z_offset(self.current_pos)
                    if abs(next_z - self.z_offset) >= self.split_delta_z:
                        self.z_offset = next_z
                        newpos = list(self.current_pos)
                        newpos[2] += self.z_offset
                        return newpos
            # end of move reached
            self.current_pos[:] = self.next_pos
            self.z_offset = self._calc_z_offset(self.current_pos)
            # Its okay to add Z-Offset to the final move, since it will not be
            # used again.
            self.current_pos[2] += self.z_offset
            self.traverse_complete = True
            return self.current_pos
        else:
            # Traverse complete
            return None


class ZMesh:
    def __init__(self, params, name):
        self.profile_name = name or "adaptive-%X" % (id(self),)
        self.probed_matrix = self.mesh_matrix = None
        self.mesh_params = params
        self.mesh_offsets = [0.0, 0.0]
        logging.debug("bed_mesh: probe/mesh parameters:")
        for key, value in self.mesh_params.items():
            logging.debug("%s :  %s" % (key, value))
        self.mesh_x_min = params["min_x"]
        self.mesh_x_max = params["max_x"]
        self.mesh_y_min = params["min_y"]
        self.mesh_y_max = params["max_y"]
        logging.debug(
            "bed_mesh: Mesh Min: (%.2f,%.2f) Mesh Max: (%.2f,%.2f)"
            % (self.mesh_x_min, self.mesh_y_min, self.mesh_x_max, self.mesh_y_max)
        )
        # Set the interpolation algorithm
        interpolation_algos = {
            "lagrange": self._sample_lagrange,
            "bicubic": self._sample_bicubic,
            "direct": self._sample_direct,
        }
        self._sample = interpolation_algos.get(params["algo"])
        # Number of points to interpolate per segment
        mesh_x_pps = params["mesh_x_pps"]
        mesh_y_pps = params["mesh_y_pps"]
        px_cnt = params["x_count"]
        py_cnt = params["y_count"]
        self.mesh_x_count = (px_cnt - 1) * mesh_x_pps + px_cnt
        self.mesh_y_count = (py_cnt - 1) * mesh_y_pps + py_cnt
        self.x_mult = mesh_x_pps + 1
        self.y_mult = mesh_y_pps + 1
        logging.debug(
            "bed_mesh: Mesh grid size - X:%d, Y:%d"
            % (self.mesh_x_count, self.mesh_y_count)
        )
        self.mesh_x_dist = (self.mesh_x_max - self.mesh_x_min) / (self.mesh_x_count - 1)
        self.mesh_y_dist = (self.mesh_y_max - self.mesh_y_min) / (self.mesh_y_count - 1)

    def get_mesh_matrix(self):
        if self.mesh_matrix is not None:
            return [[round(z, 6) for z in line] for line in self.mesh_matrix]
        return [[]]

    def get_probed_matrix(self):
        if self.probed_matrix is not None:
            return [[round(z, 6) for z in line] for line in self.probed_matrix]
        return [[]]

    def get_mesh_params(self):
        return self.mesh_params

    def get_profile_name(self):
        return self.profile_name

    def print_probed_matrix(self, print_func):
        if self.probed_matrix is not None:
            msg = "Mesh Leveling Probed Z positions:\n"
            for line in self.probed_matrix:
                for x in line:
                    msg += " %f" % x
                msg += "\n"
            print_func(msg)
        else:
            print_func("bed_mesh: bed has not been probed")

    def print_mesh(self, print_func, move_z=None):
        matrix = self.get_mesh_matrix()
        if matrix is not None:
            msg = "Mesh X,Y: %d,%d\n" % (self.mesh_x_count, self.mesh_y_count)
            if move_z is not None:
                msg += "Search Height: %d\n" % (move_z)
            msg += "Mesh Offsets: X=%.4f, Y=%.4f\n" % (
                self.mesh_offsets[0],
                self.mesh_offsets[1],
            )
            msg += "Mesh Average: %.2f\n" % (self.get_z_average())
            rng = self.get_z_range()
            msg += "Mesh Range: min=%.4f max=%.4f\n" % (rng[0], rng[1])
            msg += "Interpolation Algorithm: %s\n" % (self.mesh_params["algo"])
            msg += "Measured points:\n"
            for y_line in range(self.mesh_y_count - 1, -1, -1):
                for z in matrix[y_line]:
                    msg += "  %f" % (z)
                msg += "\n"
            print_func(msg)
        else:
            print_func("bed_mesh: Z Mesh not generated")

    def build_mesh(self, z_matrix):
        self.probed_matrix = z_matrix
        self._sample(z_matrix)
        self.print_mesh(logging.debug)

    def set_zero_reference(self, xpos, ypos):
        offset = self.calc_z(xpos, ypos)
        logging.info(
            "bed_mesh: setting zero reference at (%.2f, %.2f, %.6f)"
            % (xpos, ypos, offset)
        )
        for matrix in [self.probed_matrix, self.mesh_matrix]:
            for yidx in range(len(matrix)):
                for xidx in range(len(matrix[yidx])):
                    matrix[yidx][xidx] -= offset

    def set_mesh_offsets(self, offsets):
        for i, o in enumerate(offsets):
            if o is not None:
                self.mesh_offsets[i] = o

    def get_x_coordinate(self, index):
        return self.mesh_x_min + self.mesh_x_dist * index

    def get_y_coordinate(self, index):
        return self.mesh_y_min + self.mesh_y_dist * index

    def calc_z(self, x, y):
        if self.mesh_matrix is not None:
            tbl = self.mesh_matrix
            tx, xidx = self._get_linear_index(x + self.mesh_offsets[0], 0)
            ty, yidx = self._get_linear_index(y + self.mesh_offsets[1], 1)
            z0 = lerp(tx, tbl[yidx][xidx], tbl[yidx][xidx + 1])
            z1 = lerp(tx, tbl[yidx + 1][xidx], tbl[yidx + 1][xidx + 1])
            return lerp(ty, z0, z1)
        else:
            # No mesh table generated, no z-adjustment
            return 0.0

    def get_z_range(self):
        if self.mesh_matrix is not None:
            mesh_min = min([min(x) for x in self.mesh_matrix])
            mesh_max = max([max(x) for x in self.mesh_matrix])
            return mesh_min, mesh_max
        else:
            return 0.0, 0.0

    def get_z_average(self):
        if self.mesh_matrix is not None:
            avg_z = sum([sum(x) for x in self.mesh_matrix]) / sum(
                [len(x) for x in self.mesh_matrix]
            )
            # Round average to the nearest 100th.  This
            # should produce an offset that is divisible by common
            # z step distances
            return round(avg_z, 2)
        else:
            return 0.0

    def _get_linear_index(self, coord, axis):
        if axis == 0:
            # X-axis
            mesh_min = self.mesh_x_min
            mesh_cnt = self.mesh_x_count
            mesh_dist = self.mesh_x_dist
            cfunc = self.get_x_coordinate
        else:
            # Y-axis
            mesh_min = self.mesh_y_min
            mesh_cnt = self.mesh_y_count
            mesh_dist = self.mesh_y_dist
            cfunc = self.get_y_coordinate
        t = 0.0
        idx = int(math.floor((coord - mesh_min) / mesh_dist))
        idx = constrain(idx, 0, mesh_cnt - 2)
        t = (coord - cfunc(idx)) / mesh_dist
        return constrain(t, 0.0, 1.0), idx

    def _sample_direct(self, z_matrix):
        self.mesh_matrix = z_matrix

    def _sample_lagrange(self, z_matrix):
        x_mult = self.x_mult
        y_mult = self.y_mult
        self.mesh_matrix = [
            [
                0.0
                if ((i % x_mult) or (j % y_mult))
                else z_matrix[j // y_mult][i // x_mult]
                for i in range(self.mesh_x_count)
            ]
            for j in range(self.mesh_y_count)
        ]
        xpts, ypts = self._get_lagrange_coords()
        # Interpolate X coordinates
        for i in range(self.mesh_y_count):
            # only interpolate X-rows that have probed coordinates
            if i % y_mult != 0:
                continue
            for j in range(self.mesh_x_count):
                if j % x_mult == 0:
                    continue
                x = self.get_x_coordinate(j)
                self.mesh_matrix[i][j] = self._calc_lagrange(xpts, x, i, 0)
        # Interpolate Y coordinates
        for i in range(self.mesh_x_count):
            for j in range(self.mesh_y_count):
                if j % y_mult == 0:
                    continue
                y = self.get_y_coordinate(j)
                self.mesh_matrix[j][i] = self._calc_lagrange(ypts, y, i, 1)

    def _get_lagrange_coords(self):
        xpts = []
        ypts = []
        for i in range(self.mesh_params["x_count"]):
            xpts.append(self.get_x_coordinate(i * self.x_mult))
        for j in range(self.mesh_params["y_count"]):
            ypts.append(self.get_y_coordinate(j * self.y_mult))
        return xpts, ypts

    def _calc_lagrange(self, lpts, c, vec, axis=0):
        pt_cnt = len(lpts)
        total = 0.0
        for i in range(pt_cnt):
            n = 1.0
            d = 1.0
            for j in range(pt_cnt):
                if j == i:
                    continue
                n *= c - lpts[j]
                d *= lpts[i] - lpts[j]
            if axis == 0:
                # Calc X-Axis
                z = self.mesh_matrix[vec][i * self.x_mult]
            else:
                # Calc Y-Axis
                z = self.mesh_matrix[i * self.y_mult][vec]
            total += z * n / d
        return total

    def _sample_bicubic(self, z_matrix):
        # should work for any number of probe points above 3x3
        x_mult = self.x_mult
        y_mult = self.y_mult
        c = self.mesh_params["tension"]
        self.mesh_matrix = [
            [
                0.0
                if ((i % x_mult) or (j % y_mult))
                else z_matrix[j // y_mult][i // x_mult]
                for i in range(self.mesh_x_count)
            ]
            for j in range(self.mesh_y_count)
        ]
        # Interpolate X values
        for y in range(self.mesh_y_count):
            if y % y_mult != 0:
                continue
            for x in range(self.mesh_x_count):
                if x % x_mult == 0:
                    continue
                pts = self._get_x_ctl_pts(x, y)
                self.mesh_matrix[y][x] = self._cardinal_spline(pts, c)
        # Interpolate Y values
        for x in range(self.mesh_x_count):
            for y in range(self.mesh_y_count):
                if y % y_mult == 0:
                    continue
                pts = self._get_y_ctl_pts(x, y)
                self.mesh_matrix[y][x] = self._cardinal_spline(pts, c)

    def _get_x_ctl_pts(self, x, y):
        # Fetch control points and t for a X value in the mesh
        x_mult = self.x_mult
        x_row = self.mesh_matrix[y]
        last_pt = self.mesh_x_count - 1 - x_mult
        if x < x_mult:
            p0 = p1 = x_row[0]
            p2 = x_row[x_mult]
            p3 = x_row[2 * x_mult]
            t = x / float(x_mult)
        elif x > last_pt:
            p0 = x_row[last_pt - x_mult]
            p1 = x_row[last_pt]
            p2 = p3 = x_row[last_pt + x_mult]
            t = (x - last_pt) / float(x_mult)
        else:
            found = False
            for i in range(x_mult, last_pt, x_mult):
                if x > i and x < (i + x_mult):
                    p0 = x_row[i - x_mult]
                    p1 = x_row[i]
                    p2 = x_row[i + x_mult]
                    p3 = x_row[i + 2 * x_mult]
                    t = (x - i) / float(x_mult)
                    found = True
                    break
            if not found:
                raise BedMeshError("bed_mesh: Error finding x control points")
        return p0, p1, p2, p3, t

    def _get_y_ctl_pts(self, x, y):
        # Fetch control points and t for a Y value in the mesh
        y_mult = self.y_mult
        last_pt = self.mesh_y_count - 1 - y_mult
        y_col = self.mesh_matrix
        if y < y_mult:
            p0 = p1 = y_col[0][x]
            p2 = y_col[y_mult][x]
            p3 = y_col[2 * y_mult][x]
            t = y / float(y_mult)
        elif y > last_pt:
            p0 = y_col[last_pt - y_mult][x]
            p1 = y_col[last_pt][x]
            p2 = p3 = y_col[last_pt + y_mult][x]
            t = (y - last_pt) / float(y_mult)
        else:
            found = False
            for i in range(y_mult, last_pt, y_mult):
                if y > i and y < (i + y_mult):
                    p0 = y_col[i - y_mult][x]
                    p1 = y_col[i][x]
                    p2 = y_col[i + y_mult][x]
                    p3 = y_col[i + 2 * y_mult][x]
                    t = (y - i) / float(y_mult)
                    found = True
                    break
            if not found:
                raise BedMeshError("bed_mesh: Error finding y control points")
        return p0, p1, p2, p3, t

    def _cardinal_spline(self, p, tension):
        t = p[4]
        t2 = t * t
        t3 = t2 * t
        m1 = tension * (p[2] - p[0])
        m2 = tension * (p[3] - p[1])
        a = p[1] * (2 * t3 - 3 * t2 + 1)
        b = p[2] * (-2 * t3 + 3 * t2)
        c = m1 * (t3 - 2 * t2 + t)
        d = m2 * (t3 - t2)
        return a + b + c + d


class ProfileManager:
    def __init__(self, config, bedmesh):
        self.name = config.get_name()
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.bedmesh = bedmesh
        self.profiles = {}
        self.incompatible_profiles = []
        # Fetch stored profiles from Config
        stored_profs = config.get_prefix_sections(self.name)
        stored_profs = [s for s in stored_profs if s.get_name() != self.name]
        for profile in stored_profs:
            name = profile.get_name().split(" ", 1)[1]
            version = profile.getint("version", 0)
            if version != PROFILE_VERSION:
                logging.info(
                    "bed_mesh: Profile [%s] not compatible with this version\n"
                    "of bed_mesh.  Profile Version: %d Current Version: %d "
                    % (name, version, PROFILE_VERSION)
                )
                self.incompatible_profiles.append(name)
                continue
            self.profiles[name] = {}
            zvals = profile.getlists("points", seps=(",", "\n"), parser=float)
            self.profiles[name]["points"] = zvals
            self.profiles[name]["mesh_params"] = params = collections.OrderedDict()
            for key, t in PROFILE_OPTIONS.items():
                if t is int:
                    params[key] = profile.getint(key)
                elif t is float:
                    params[key] = profile.getfloat(key)
                elif t is str:
                    params[key] = profile.get(key)
        # Register GCode
        self.gcode.register_command(
            "BED_MESH_PROFILE",
            self.cmd_BED_MESH_PROFILE,
            desc=self.cmd_BED_MESH_PROFILE_help,
        )

    def get_profiles(self):
        return self.profiles

    def _check_incompatible_profiles(self):
        if self.incompatible_profiles:
            configfile = self.printer.lookup_object("configfile")
            for profile in self.incompatible_profiles:
                configfile.remove_section("bed_mesh " + profile)
            self.gcode.respond_info(
                "The following incompatible profiles have been detected\n"
                "and are scheduled for removal:\n%s\n"
                "The SAVE_CONFIG command will update the printer config\n"
                "file and restart the printer"
                % (("\n").join(self.incompatible_profiles))
            )

    def save_profile(self, prof_name):
        z_mesh = self.bedmesh.get_mesh()
        if z_mesh is None:
            self.gcode.respond_info(
                "Unable to save to profile [%s], the bed has not been probed"
                % (prof_name)
            )
            return
        probed_matrix = z_mesh.get_probed_matrix()
        mesh_params = z_mesh.get_mesh_params()
        configfile = self.printer.lookup_object("configfile")
        cfg_name = self.name + " " + prof_name
        # set params
        z_values = ""
        for line in probed_matrix:
            z_values += "\n  "
            for p in line:
                z_values += "%.6f, " % p
            z_values = z_values[:-2]
        configfile.set(cfg_name, "version", PROFILE_VERSION)
        configfile.set(cfg_name, "points", z_values)
        for key, value in mesh_params.items():
            configfile.set(cfg_name, key, value)
        # save copy in local storage
        # ensure any self.profiles returned as status remains immutable
        profiles = dict(self.profiles)
        profiles[prof_name] = profile = {}
        profile["points"] = probed_matrix
        profile["mesh_params"] = collections.OrderedDict(mesh_params)
        self.profiles = profiles
        self.bedmesh.update_status()
        self.gcode.respond_info(
            "Bed Mesh state has been saved to profile [%s]\n"
            "for the current session.  The SAVE_CONFIG command will\n"
            "update the printer config file and restart the printer." % (prof_name)
        )

    def load_profile(self, prof_name):
        profile = self.profiles.get(prof_name, None)
        if profile is None:
            raise self.gcode.error("bed_mesh: Unknown profile [%s]" % prof_name)
        probed_matrix = profile["points"]
        mesh_params = profile["mesh_params"]
        z_mesh = ZMesh(mesh_params, prof_name)
        try:
            z_mesh.build_mesh(probed_matrix)
        except BedMeshError as e:
            raise self.gcode.error(str(e))
        self.bedmesh.set_mesh(z_mesh)

    def remove_profile(self, prof_name):
        if prof_name in self.profiles:
            configfile = self.printer.lookup_object("configfile")
            configfile.remove_section("bed_mesh " + prof_name)
            profiles = dict(self.profiles)
            del profiles[prof_name]
            self.profiles = profiles
            self.bedmesh.update_status()
            self.gcode.respond_info(
                "Profile [%s] removed from storage for this session.\n"
                "The SAVE_CONFIG command will update the printer\n"
                "configuration and restart the printer" % (prof_name)
            )
        else:
            self.gcode.respond_info("No profile named [%s] to remove" % (prof_name))

    cmd_BED_MESH_PROFILE_help = "Bed Mesh Persistent Storage management"

    def cmd_BED_MESH_PROFILE(self, gcmd):
        options = collections.OrderedDict(
            {
                "LOAD": self.load_profile,
                "SAVE": self.save_profile,
                "REMOVE": self.remove_profile,
            }
        )
        for key in options:
            name = gcmd.get(key, None)
            if name is not None:
                if not name.strip():
                    raise gcmd.error(
                        "Value for parameter '%s' must be specified" % (key)
                    )
                if name == "default" and key == "SAVE":
                    gcmd.respond_info(
                        "Profile 'default' is reserved, please choose"
                        " another profile name."
                    )
                else:
                    options[key](name)
                return
        gcmd.respond_info("Invalid syntax '%s'" % (gcmd.get_commandline(),))


def load_config(config):
    return BedMesh(config)
