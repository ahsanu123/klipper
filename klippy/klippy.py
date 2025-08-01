#!/usr/bin/env python2
# Main code for host side printer firmware
#
# Copyright (C) 2016-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys
import os
import gc
import optparse
import logging
import time
import collections
import importlib
import util
import reactor
import queuelogger
import msgproto
import gcode
import configfile
import pins
import mcu
import toolhead
import webhooks

message_ready = "Printer is ready"

message_startup = """
Printer is not ready
The klippy host software is attempting to connect.  Please
retry in a few moments.
"""

message_restart = """
Once the underlying issue is corrected, use the "RESTART"
command to reload the config and restart the host software.
Printer is halted
"""


class Printer:
    config_error = configfile.error
    command_error = gcode.CommandError

    def __init__(self, main_reactor, bglogger, start_args):
        self.bglogger = bglogger
        self.start_args = start_args
        self.reactor = main_reactor
        self.reactor.register_callback(self._connect)
        self.state_message = message_startup
        self.in_shutdown_state = False
        self.run_result = None
        self.event_handlers = {}
        self.objects = collections.OrderedDict()
        # Init printer components that must be setup prior to config
        for m in [gcode, webhooks]:
            m.add_early_printer_objects(self)

    def get_start_args(self):
        return self.start_args

    def get_reactor(self):
        return self.reactor

    def get_state_message(self):
        if self.state_message == message_ready:
            category = "ready"
        elif self.state_message == message_startup:
            category = "startup"
        elif self.in_shutdown_state:
            category = "shutdown"
        else:
            category = "error"
        return self.state_message, category

    def is_shutdown(self):
        return self.in_shutdown_state

    def _set_state(self, msg):
        if self.state_message in (message_ready, message_startup):
            self.state_message = msg
        if msg != message_ready and self.start_args.get("debuginput") is not None:
            self.request_exit("error_exit")

    def update_error_msg(self, oldmsg, newmsg):
        if (
            self.state_message != oldmsg
            or self.state_message in (message_ready, message_startup)
            or newmsg in (message_ready, message_startup)
        ):
            return
        self.state_message = newmsg
        logging.error(newmsg)

    def add_object(self, name, obj):
        if name in self.objects:
            raise self.config_error("Printer object '%s' already created" % (name,))
        self.objects[name] = obj

    def lookup_object(self, name, default=configfile.sentinel):
        if name in self.objects:
            return self.objects[name]
        if default is configfile.sentinel:
            raise self.config_error("Unknown config object '%s'" % (name,))
        return default

    def lookup_objects(self, module=None):
        if module is None:
            return list(self.objects.items())
        prefix = module + " "
        objs = [(n, self.objects[n]) for n in self.objects if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + objs
        return objs

    def load_object(self, config, section, default=configfile.sentinel):
        if section in self.objects:
            return self.objects[section]
        module_parts = section.split()
        module_name = module_parts[0]
        py_name = os.path.join(os.path.dirname(__file__), "extras", module_name + ".py")
        py_dirname = os.path.join(
            os.path.dirname(__file__), "extras", module_name, "__init__.py"
        )
        if not os.path.exists(py_name) and not os.path.exists(py_dirname):
            if default is not configfile.sentinel:
                return default
            raise self.config_error("Unable to load module '%s'" % (section,))
        mod = importlib.import_module("extras." + module_name)
        init_func = "load_config"
        if len(module_parts) > 1:
            init_func = "load_config_prefix"
        init_func = getattr(mod, init_func, None)
        if init_func is None:
            if default is not configfile.sentinel:
                return default
            raise self.config_error("Unable to load module '%s'" % (section,))
        self.objects[section] = init_func(config.getsection(section))
        return self.objects[section]

    def _read_config(self):
        self.objects["configfile"] = pconfig = configfile.PrinterConfig(self)
        config = pconfig.read_main_config()
        if self.bglogger is not None:
            pconfig.log_config(config)
        # Create printer components
        for m in [pins, mcu]:
            m.add_printer_objects(config)
        for section_config in config.get_prefix_sections(""):
            self.load_object(config, section_config.get_name(), None)
        for m in [toolhead]:
            m.add_printer_objects(config)
        # Validate that there are no undefined parameters in the config file
        pconfig.check_unused_options(config)

    def _connect(self, eventtime):
        try:
            self._read_config()
            self.send_event("klippy:mcu_identify")
            for cb in self.event_handlers.get("klippy:connect", []):
                if self.state_message is not message_startup:
                    return
                cb()
        except (self.config_error, pins.error) as e:
            logging.exception("Config error")
            self._set_state("%s\n%s" % (str(e), message_restart))
            return
        except msgproto.error as e:
            msg = "Protocol error"
            logging.exception(msg)
            self._set_state(msg)
            self.send_event("klippy:notify_mcu_error", msg, {"error": str(e)})
            util.dump_mcu_build()
            return
        except mcu.error as e:
            msg = "MCU error during connect"
            logging.exception(msg)
            self._set_state(msg)
            self.send_event("klippy:notify_mcu_error", msg, {"error": str(e)})
            util.dump_mcu_build()
            return
        except Exception as e:
            logging.exception("Unhandled exception during connect")
            self._set_state(
                "Internal error during connect: %s\n%s"
                % (
                    str(e),
                    message_restart,
                )
            )
            return
        try:
            self._set_state(message_ready)
            for cb in self.event_handlers.get("klippy:ready", []):
                if self.state_message is not message_ready:
                    return
                cb()
        except Exception as e:
            logging.exception("Unhandled exception during ready callback")
            self.invoke_shutdown("Internal error during ready callback: %s" % (str(e),))

    def run(self):
        systime = time.time()
        monotime = self.reactor.monotonic()
        logging.info(
            "Start printer at %s (%.1f %.1f)",
            time.asctime(time.localtime(systime)),
            systime,
            monotime,
        )
        # Enter main reactor loop
        try:
            self.reactor.run()
        except:
            msg = "Unhandled exception during run"
            logging.exception(msg)
            # Exception from a reactor callback - try to shutdown
            try:
                self.reactor.register_callback((lambda e: self.invoke_shutdown(msg)))
                self.reactor.run()
            except:
                logging.exception("Repeat unhandled exception during run")
                # Another exception - try to exit
                self.run_result = "error_exit"
        # Check restart flags
        run_result = self.run_result
        try:
            if run_result == "firmware_restart":
                self.send_event("klippy:firmware_restart")
            self.send_event("klippy:disconnect")
        except:
            logging.exception("Unhandled exception during post run")
        return run_result

    def set_rollover_info(self, name, info, log=True):
        if log:
            logging.info(info)
        if self.bglogger is not None:
            self.bglogger.set_rollover_info(name, info)

    def invoke_shutdown(self, msg, details={}):
        if self.in_shutdown_state:
            return
        logging.error("Transition to shutdown state: %s", msg)
        self.in_shutdown_state = True
        self._set_state(msg)
        for cb in self.event_handlers.get("klippy:shutdown", []):
            try:
                cb()
            except:
                logging.exception("Exception during shutdown handler")
        logging.info("Reactor garbage collection: %s", self.reactor.get_gc_stats())
        self.send_event("klippy:notify_mcu_shutdown", msg, details)

    def invoke_async_shutdown(self, msg, details={}):
        self.reactor.register_async_callback(
            (lambda e: self.invoke_shutdown(msg, details))
        )

    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *params):
        return [cb(*params) for cb in self.event_handlers.get(event, [])]

    def request_exit(self, result):
        if self.run_result is None:
            self.run_result = result
        self.reactor.end()


######################################################################
# Startup
######################################################################


def import_test():
    # Import all optional modules (used as a build test)
    dname = os.path.dirname(__file__)
    for mname in ["extras", "kinematics"]:
        for fname in os.listdir(os.path.join(dname, mname)):
            if fname.endswith(".py") and fname != "__init__.py":
                module_name = fname[:-3]
            else:
                iname = os.path.join(dname, mname, fname, "__init__.py")
                if not os.path.exists(iname):
                    continue
                module_name = fname
            importlib.import_module(mname + "." + module_name)
    sys.exit(0)


def arg_dictionary(option, opt_str, value, parser):
    key, fname = "dictionary", value
    if "=" in value:
        mcu_name, fname = value.split("=", 1)
        key = "dictionary_" + mcu_name
    if parser.values.dictionary is None:
        parser.values.dictionary = {}
    parser.values.dictionary[key] = fname


def main():
    usage = "%prog [options] <config file>"
    opts = optparse.OptionParser(usage)
    opts.add_option(
        "-i",
        "--debuginput",
        dest="debuginput",
        help="read commands from file instead of from tty port",
    )
    opts.add_option(
        "-I",
        "--input-tty",
        dest="inputtty",
        default="/tmp/printer",
        help="input tty name (default is /tmp/printer)",
    )
    opts.add_option(
        "-a",
        "--api-server",
        dest="apiserver",
        help="api server unix domain socket filename",
    )
    opts.add_option(
        "-l", "--logfile", dest="logfile", help="write log to file instead of stderr"
    )
    opts.add_option(
        "-v", action="store_true", dest="verbose", help="enable debug messages"
    )
    opts.add_option(
        "-o",
        "--debugoutput",
        dest="debugoutput",
        help="write output to file instead of to serial port",
    )
    opts.add_option(
        "-d",
        "--dictionary",
        dest="dictionary",
        type="string",
        action="callback",
        callback=arg_dictionary,
        help="file to read for mcu protocol dictionary",
    )
    opts.add_option(
        "--import-test", action="store_true", help="perform an import module test"
    )
    options, args = opts.parse_args()
    if options.import_test:
        import_test()
    if len(args) != 1:
        opts.error("Incorrect number of arguments")
    start_args = {
        "config_file": args[0],
        "apiserver": options.apiserver,
        "start_reason": "startup",
    }

    debuglevel = logging.INFO
    if options.verbose:
        debuglevel = logging.DEBUG
    if options.debuginput:
        start_args["debuginput"] = options.debuginput
        debuginput = open(options.debuginput, "rb")
        start_args["gcode_fd"] = debuginput.fileno()
    else:
        start_args["gcode_fd"] = util.create_pty(options.inputtty)
    if options.debugoutput:
        start_args["debugoutput"] = options.debugoutput
        start_args.update(options.dictionary)
    bglogger = None
    if options.logfile:
        start_args["log_file"] = options.logfile
        bglogger = queuelogger.setup_bg_logging(options.logfile, debuglevel)
    else:
        logging.getLogger().setLevel(debuglevel)
    logging.info("Starting Klippy...")
    git_info = util.get_git_version()
    git_vers = git_info["version"]
    extra_files = [
        fname
        for code, fname in git_info["file_status"]
        if (
            code in ("??", "!!")
            and fname.endswith(".py")
            and (
                fname.startswith("klippy/kinematics/")
                or fname.startswith("klippy/extras/")
            )
        )
    ]
    modified_files = [fname for code, fname in git_info["file_status"] if code == "M"]
    extra_git_desc = ""
    if extra_files:
        if not git_vers.endswith("-dirty"):
            git_vers = git_vers + "-dirty"
        if len(extra_files) > 10:
            extra_files[10:] = ["(+%d files)" % (len(extra_files) - 10,)]
        extra_git_desc += "\nUntracked files: %s" % (", ".join(extra_files),)
    if modified_files:
        if len(modified_files) > 10:
            modified_files[10:] = ["(+%d files)" % (len(modified_files) - 10,)]
        extra_git_desc += "\nModified files: %s" % (", ".join(modified_files),)
    extra_git_desc += "\nBranch: %s" % (git_info["branch"])
    extra_git_desc += "\nRemote: %s" % (git_info["remote"])
    extra_git_desc += "\nTracked URL: %s" % (git_info["url"])
    start_args["software_version"] = git_vers
    start_args["cpu_info"] = util.get_cpu_info()
    if bglogger is not None:
        versions = "\n".join(
            [
                "Args: %s" % (sys.argv,),
                "Git version: %s%s"
                % (repr(start_args["software_version"]), extra_git_desc),
                "CPU: %s" % (start_args["cpu_info"],),
                "Python: %s" % (repr(sys.version),),
            ]
        )
        logging.info(versions)
    elif not options.debugoutput:
        logging.warning("No log file specified! Severe timing issues may result!")
    gc.disable()

    # Start Printer() class
    while 1:
        if bglogger is not None:
            bglogger.clear_rollover_info()
            bglogger.set_rollover_info("versions", versions)
        gc.collect()
        main_reactor = reactor.Reactor(gc_checking=True)
        printer = Printer(main_reactor, bglogger, start_args)
        res = printer.run()
        if res in ["exit", "error_exit"]:
            break
        time.sleep(1.0)
        main_reactor.finalize()
        main_reactor = printer = None
        logging.info("Restarting printer")
        start_args["start_reason"] = res

    if bglogger is not None:
        bglogger.stop()

    if res == "error_exit":
        sys.exit(-1)


if __name__ == "__main__":
    main()
