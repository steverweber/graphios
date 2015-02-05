#!/usr/bin/python -tt
# vim: set ts=4 sw=4 tw=79 et :
# Copyright (C) 2011  Shawn Sterling <shawn@systemtemplar.org>
#
# With contributions from:
#
# Juan Jose Presa <juanjop@gmail.com>
# Ranjib Dey <dey.ranjib@gmail.com>
# Ryan Davis <https://github.com/ryepup>
# Alexey Diyan <alexey.diyan@gmail.com>
# Steffen Zieger <me@saz.sh>
# Nathan Bird <ecthellion@gmail.com>
# Dave Josephsen <dave@skeptech.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.
#
# graphios: this program will read nagios host and service perfdata, and
# send it to a carbon server.
#
# The latest version of this code will be found on my github page:
# https://github.com/shawn-sterling

from ConfigParser import SafeConfigParser
from optparse import OptionParser
import copy
import graphios_backends as backends
import logging
import logging.handlers
import os
import os.path
import re
import sys
import time


# ##########################################################
# ###  Do not edit this file, edit the graphios.cfg    #####

# nagios spool directory
spool_directory = '/var/spool/nagios/graphios'

# graphios log info
log_file = ''
log_max_size = 25165824         # 24 MB

# by default we will check the current path for graphios.cfg, if config_file
# is passed as a command line argument we will use that instead.
config_file = ''

# This is overridden via config file
debug = False

# config dictionary
cfg = {}

# backend global
be = ""

# options parsing
parser = OptionParser("""usage: %prog [options]
sends nagios performance data to carbon.
""")

parser.add_option('-v', "--verbose", action="store_true", dest="verbose",
                  help="sets logging to DEBUG level")
parser.add_option("--spool-directory", dest="spool_directory",
                  default=spool_directory,
                  help="where to look for nagios performance data")
parser.add_option("--log-file", dest="log_file",
                  default=log_file,
                  help="file to log to")
parser.add_option("--backend", dest="backend", default="stdout",
                  help="sets which storage backend to use")
parser.add_option("--config_file", dest="config_file", default="",
                  help="set custom config file location")
parser.add_option("--test", action="store_true", dest="test", default="",
                  help="Turns on test mode, which won't send to backends")
parser.add_option("--replace_char", dest="replace_char", default="_",
                  help="Replacement Character (default '_'")
parser.add_option("--sleep_time", dest="sleep_time", default=15,
                  help="How much time to sleep between checks")
parser.add_option("--sleep_max", dest="sleep_max", default=480,
                  help="Max time to sleep between runs")
parser.add_option("--server", dest="server", default="",
                  help="Server address (for backend)")
parser.add_option("--no_replace_hostname", action="store_false",
                  dest="replace_hostname", default=True,
                  help="Replace '.' in nagios hostnames, default on.")
parser.add_option("--reverse_hostname", action="store_true",
                  dest="reverse_hostname",
                  help="Reverse nagios hostname, default off.")


log = logging.getLogger('log')


class GraphiosMetric(object):
    def __init__(self):
        self.LABEL = ''                 # The name in the perfdata from nagios
        self.VALUE = ''                 # The measured value of that metric
        self.UOM = ''                   # The unit of measure for the metric
        self.DATATYPE = ''              # HOSTPERFDATA|SERVICEPERFDATA
        self.METRICTYPE = 'gauge'       # gauge|counter|timer etc..
        self.TIMET = ''                 # Epoc time the measurement was taken
        self.HOSTNAME = ''              # name of th host measured
        self.SERVICEDESC = ''           # nagios configured service description
        self.PERFDATA = ''              # the space-delimited raw perfdata
        self.SERVICECHECKCOMMAND = ''   # literal check command syntax
        self.HOSTCHECKCOMMAND = ''      # literal check command syntax
        self.HOSTSTATE = ''             # current state afa nagios is concerned
        self.HOSTSTATETYPE = ''         # HARD|SOFT
        self.SERVICESTATE = ''          # current state afa nagios is concerned
        self.SERVICESTATETYPE = ''      # HARD|SOFT
        self.GRAPHITEPREFIX = ''        # graphios prefix
        self.GRAPHITEPOSTFIX = ''       # graphios suffix
        self.VALID = False              # if this metric is valid

    def validate(self):
        # because we eliminated all whitespace, there shouldn't be any quotes
        # this happens more with windows nagios plugins
        re.sub("'", "", self.LABEL)
        re.sub('"', "", self.LABEL)
        re.sub("'", "", self.VALUE)
        re.sub('"', "", self.VALUE)
        self.check_adjust_hostname()
        if (
            self.TIMET is not '' and
            self.PERFDATA is not '' and
            self.HOSTNAME is not ''
        ):
            if "use_service_desc" in cfg and cfg["use_service_desc"] is True:
                if self.SERVICEDESC != '' or self.DATATYPE == 'HOSTPERFDATA':
                    self.VALID = True
            else:
                # not using service descriptions
                if (
                    self.GRAPHITEPREFIX == "" and
                    self.GRAPHITEPOSTFIX == ""
                ):
                    self.VALID = False
                else:
                    self.VALID = True

    def check_adjust_hostname(self):
        if cfg["reverse_hostname"]:
            self.HOSTNAME = '.'.join(reversed(self.HOSTNAME.split('.')))
        if cfg["replace_hostname"]:
            self.HOSTNAME = self.HOSTNAME.replace(".",
                                                  cfg["replacement_character"])


def chk_bool(value):
    """
    checks if value is a stringified boolean
    """
    if (value.lower() == "true"):
        return True
    elif (value.lower() == "false"):
        return False
    return value


def read_config(config_file):
    """
    reads the config file
    """
    if config_file == '':
        # check same dir as graphios binary
        my_file = "%s/graphios.cfg" % sys.path[0]
        if os.path.isfile(my_file):
            config_file = my_file
        else:
            # check /etc/graphios/graphios.cfg
            config_file = "/etc/graphios/graphios.cfg"
    config = SafeConfigParser()
    # The logger won't be initialized yet, so we use print_debug
    if os.path.isfile(config_file):
        config.read(config_file)
        config_dict = {}
        for section in config.sections():
            # there should only be 1 'graphios' section
            print_debug("section: %s" % section)
            config_dict['name'] = section
            for name, value in config.items(section):
                config_dict[name] = chk_bool(value)
                print_debug("config[%s]=%s" % (name, value))
        # print config_dict
        return config_dict
    else:
        print_debug("Can't open config file: %s" % config_file)
        print """\nEither modify the script at the config_file = '' line and
specify where you want your config file to be, or create a config file
in the above directory (which should be the same dir the graphios.py is in)
or you can specify --config=myconfigfilelocation at the command line."""
        sys.exit(1)


def verify_config(config_dict):
    """
    verifies the required config variables are found
    """
    global spool_directory
    ensure_list = ['replacement_character', 'log_file', 'log_max_size',
                   'log_level', 'sleep_time', 'sleep_max', 'test_mode']
    missing_values = []
    for ensure in ensure_list:
        if ensure not in config_dict:
            missing_values.append(ensure)
    if len(missing_values) > 0:
        print "\nMust have value in config file for:\n"
        for value in missing_values:
            print "%s\n" % value
        sys.exit(1)
    if "spool_directory" in config_dict:
        spool_directory = config_dict['spool_directory']


def print_debug(msg):
    """
    prints a debug message if global debug is True
    """
    if debug:
        print msg


def verify_options(opts):
    """
    verify the passed command line options, puts into global cfg
    """
    global cfg
    global spool_directory
    # because these have defaults in the parser section we know they will be
    # set. So we don't have to do a bunch of ifs.
    if "log_file" not in cfg:
        cfg["log_file"] = opts.log_file
    if cfg["log_file"] == "''" or cfg["log_file"] == "":
        cfg["log_file"] = "%s/graphios.log" % sys.path[0]
    cfg["log_max_size"] = 25165824         # 24 MB
    if opts.verbose:
        cfg["debug"] = True
        cfg["log_level"] = "logging.DEBUG"
    else:
        cfg["debug"] = False
        cfg["log_level"] = "logging.INFO"
    if opts.test:
        cfg["test_mode"] = True
    else:
        cfg["test_mode"] = False
    cfg["replacement_character"] = opts.replace_char
    cfg["spool_directory"] = opts.spool_directory
    cfg["sleep_time"] = opts.sleep_time
    cfg["sleep_max"] = opts.sleep_max
    cfg["replace_hostname"] = opts.replace_hostname
    cfg["reverse_hostname"] = opts.reverse_hostname
    spool_directory = opts.spool_directory
    # cfg["backend"] = opts.backend
    handle_backends(opts)
    # cfg["enable_carbon"] = True
    return cfg


def handle_backends(opts):
    global cfg
    if opts.backend == "carbon" or opts.backend == "statsd":
        if not opts.server:
            print "Must also have --server for carbon or statsd."
            sys.exit(1)
        if opts.backend == "carbon":
            cfg["enable_carbon"] = True
            cfg["carbon_servers"] = opts.server
        if opts.backend == "statsd":
            cfg["enable_statsd"] = True
            cfg["statsd_server"] = opts.server
    if opts.backend == "librato":
        print "Use graphios.cfg for librato."
        sys.exit(1)


def configure():
    """
    sets up graphios config
    """
    global debug
    log_handler = logging.handlers.RotatingFileHandler(
        cfg["log_file"], maxBytes=cfg["log_max_size"], backupCount=4,
        # encoding='bz2')
    )
    formatter = logging.Formatter(
        "%(asctime)s %(filename)s %(levelname)s %(message)s",
        "%B %d %H:%M:%S")
    log_handler.setFormatter(formatter)
    log.addHandler(log_handler)

    if "debug" in cfg and cfg["debug"] is True:
        log.debug("adding streamhandler")
        log.setLevel(logging.DEBUG)
        log.addHandler(logging.StreamHandler())
        debug = True
    else:
        log.setLevel(logging.INFO)
        debug = False


def process_log(file_name):
    """ process log lines into GraphiosMetric Objects.
    input is a tab delimited series of key/values each of which are delimited
    by '::' it looks like:
    DATATYPE::HOSTPERFDATA  TIMET::1399738074 etc..
    """
    processed_objects = []  # the final list of metric objects we'll return
    graphite_lines = 0  # count the number of valid lines we process
    try:
        host_data_file = open(file_name, "r")
        file_array = host_data_file.readlines()
        host_data_file.close()
    except (IOError, OSError) as ex:
        log.critical("Can't open file:%s error: %s" % (file_name, ex))
        sys.exit(2)
    # parse each line into a metric object
    for line in file_array:
        if not re.search("^DATATYPE::", line):
            continue
        # log.debug('parsing: %s' % line)
        graphite_lines += 1
        variables = line.split('\t')
        mobj = get_mobj(variables)
        if mobj:
            # break out the metric object into one object per perfdata metric
            # log.debug('perfdata:%s' % mobj.PERFDATA)
            for metric in mobj.PERFDATA.split():
                try:
                    nobj = copy.copy(mobj)
                    (nobj.LABEL, d) = metric.split('=')
                    v = d.split(';')[0]
                    u = v
                    nobj.VALUE = re.sub("[a-zA-Z%]", "", v)
                    nobj.UOM = re.sub("[^a-zA-Z]+", "", u)
                    processed_objects.append(nobj)
                except:
                    log.critical("failed to parse label: '%s' part of perf"
                                 "string '%s'" % (metric, nobj.PERFDATA))
                    continue
    return processed_objects


def get_mobj(nag_array):
    """
        takes a split array of nagios variables and returns a mobj if it's
        valid. otherwise return False.
    """
    mobj = GraphiosMetric()
    for var in nag_array:
        # drop the metric if we can't split it for any reason
        try:
            (var_name, value) = var.split('::', 1)
        except:
            log.warn("could not split value %s, dropping metric" % var)
            return False

        value = re.sub("/", cfg["replacement_character"], value)
        if re.search("PERFDATA", var_name):
            mobj.PERFDATA = value
        elif re.search("^\$_", value):
            continue
        else:
            value = re.sub("\s", "", value)
            setattr(mobj, var_name, value)
    mobj.validate()
    if mobj.VALID is True:
        return mobj
    return False


def handle_file(file_name, graphite_lines):
    """
    archive processed metric lines and delete the input log files
    """
    if "test_mode" in cfg and cfg["test_mode"] is True:
        log.debug("graphite_lines:%s" % graphite_lines)
    else:
        try:
            os.remove(file_name)
        except (OSError, IOError) as ex:
            log.critical("couldn't remove file %s error:%s" % (file_name, ex))
        else:
            log.debug("deleted %s" % file_name)


def process_spool_dir(directory):
    """
    processes the files in the spool directory
    """
    global be
    log.debug("Processing spool directory %s", directory)
    num_files = 0
    mobjs_len = 0
    try:
        perfdata_files = os.listdir(directory)
    except (IOError, OSError) as e:
        print "Exception '%s' reading spool directory: %s" % (e, directory)
        print "Check if dir exists, or file permissions."
        print "Exiting."
        sys.exit(1)
    for perfdata_file in perfdata_files:
        mobjs = []
        processed_dict = {}
        all_done = True
        file_dir = os.path.join(directory, perfdata_file)
        if check_skip_file(perfdata_file, file_dir):
            continue
        num_files += 1
        mobjs = process_log(file_dir)
        mobjs_len = len(mobjs)
        processed_dict = send_backends(mobjs)
        # process the output from the backends and decide the fate of the file
        for backend in be["essential_backends"]:
            if processed_dict[backend] < mobjs_len:
                log.critical("keeping %s, insufficent metrics sent from %s. \
                             Should be %s, got %s" % (file_dir, backend,
                                                      mobjs_len,
                                                      processed_dict[backend]))
                all_done = False
        if all_done is True:
            handle_file(file_dir, len(mobjs))
    log.info("Processed %s files (%s metrics) in %s" % (num_files,
             mobjs_len, directory))


def check_skip_file(file_name, file_dir):
    """
    checks if file should be skipped
    """
    if (
        file_name == "host-perfdata" or
        file_name == "service-perfdata"
    ):
        return True
    elif re.match('^_', file_name):
        return True

    if os.stat(file_dir)[6] == 0:
        # file was 0 bytes
        handle_file(file_dir, 0)
        return True
    if os.path.isdir(file_dir):
        return True
    return False


def init_backends():
    """
    I'm going to be a little forward thinking with this and build a global dict
    of enabled back-ends whose values are instantiations of the back-end
    objects themselves. I know, global bad, but hypothetically we could modify
    this dict dynamically via a runtime-interface (graphiosctl?) to turn off/on
    backends without having to restart the graphios process.  I feel like
    that's enough of a win to justify the global. If you build it, they will
    come.
    """
    global be
    be = {}  # a top-level global for important backend-related stuff
    be["enabled_backends"] = {}  # a dict of instantiated backend objects
    be["essential_backends"] = []  # a list of backends we actually care about
    # PLUGIN WRITERS! register your new backends by adding their obj name here
    avail_backends = ("carbon",
                      "statsd",
                      "librato",
                      "stdout",
                      )
    # populate the controller dict from avail + config. this assumes you named
    # your backend the same as the config option that enables your backend (eg.
    # carbon and enable_carbon)
    for backend in avail_backends:
        cfg_option = "enable_%s" % (backend)
        if cfg_option in cfg and cfg[cfg_option] is True:
            backend_obj = getattr(backends, backend)
            be["enabled_backends"][backend] = backend_obj(cfg)
            nerf_option = "nerf_%s" % (backend)
            if nerf_option in cfg:
                if cfg[nerf_option] is False:
                    be["essential_backends"].append(backend)
            else:
                be["essential_backends"].append(backend)
    # not proud of that slovenly conditional ^^
    log.info("Enabled backends: %s" % be["enabled_backends"].keys())


def send_backends(metrics):
    """
    use the enabled_backends dict to call into the backend send functions
    """
    global be
    if len(be["enabled_backends"]) < 1:
        log.critical("At least one Back-end must be enabled in graphios.cfg")
        sys.exit(1)
    ret = {}  # return a dict of who processed what
    processed_lines = 0
    for backend in be["enabled_backends"]:
        processed_lines = be["enabled_backends"][backend].send(metrics)
        # log.debug('%s processed %s metrics' % backend, processed_lines)
        ret[backend] = processed_lines
    return ret


def main():
    log.info("graphios startup.")
    try:
        while True:
            process_spool_dir(spool_directory)
            log.debug("graphios sleeping.")
            time.sleep(float(cfg["sleep_time"]))
    except KeyboardInterrupt:
        log.info("ctrl-c pressed. Exiting graphios.")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        (options, args) = parser.parse_args()
        # print options
        if options.config_file:
            cfg = read_config(options.config_file)
        else:
            cfg = verify_options(options)
    else:
        cfg = read_config(config_file)
    verify_config(cfg)
    configure()
    # print cfg
    init_backends()
    main()
