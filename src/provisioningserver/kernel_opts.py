# Copyright 2012-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Generate kernel command-line options for inclusion in PXE configs."""

__all__ = [
    'compose_kernel_command_line',
    'KernelParameters',
    'prefix_target_name',
    ]

from collections import namedtuple
import os

import curtin
from netaddr import IPAddress
from provisioningserver.drivers import ArchitectureRegistry
from provisioningserver.logger import get_maas_logger


maaslog = get_maas_logger("kernel_opts")


class EphemeralImagesDirectoryNotFound(Exception):
    """The ephemeral images directory cannot be found."""


KernelParametersBase = namedtuple(
    "KernelParametersBase", (
        "osystem",  # Operating system, e.g. "ubuntu"
        "arch",  # Machine architecture, e.g. "i386"
        "subarch",  # Machine subarchitecture, e.g. "generic"
        "release",  # OS release, e.g. "precise"
        "label",  # Image label, e.g. "release"
        "purpose",  # Boot purpose, e.g. "commissioning"
        "hostname",  # Machine hostname, e.g. "coleman"
        "domain",  # Machine domain name, e.g. "example.com"
        "preseed_url",  # URL from which a preseed can be obtained.
        "log_host",  # Host/IP to which syslog can be streamed.
        "fs_host",  # Host/IP on which ephemeral filesystems are hosted.
        "extra_opts",  # String of extra options to supply, will be appended
                       # verbatim to the kernel command line
        ))


class KernelParameters(KernelParametersBase):

    # foo._replace() is just ugly, so alias it to __call__.
    __call__ = KernelParametersBase._replace


def compose_preseed_opt(preseed_url):
    """Compose a kernel option for preseed URL.

    :param preseed_url: The URL from which a preseed can be fetched.
    """
    # See https://help.ubuntu.com/12.04/installation-guide
    #   /i386/preseed-using.html#preseed-auto
    return "auto url=%s" % preseed_url


def compose_locale_opt():
    locale = 'en_US'
    return "locale=%s" % locale


def compose_logging_opts(log_host):
    return [
        'log_host=%s' % log_host,
        'log_port=%d' % 514,
        ]


def get_last_directory(root):
    """Return the last directory from the directories in the given root.

    This is used to get the most recent ephemeral import directory.
    The ephemeral directories are named after the release date: 20120424,
    20120424, 20120301, etc. so fetching the last one (sorting by name)
    returns the most recent.
    """
    dirs = (os.path.join(root, directory) for directory in os.listdir(root))
    dirs = filter(os.path.isdir, dirs)
    return max(dirs)


ISCSI_TARGET_NAME_PREFIX = "iqn.2004-05.com.ubuntu:maas"


def get_ephemeral_name(osystem, arch, subarch, release, label):
    """Return the name of the most recent ephemeral image."""
    return "ephemeral-%s-%s-%s-%s-%s" % (
        osystem,
        arch,
        subarch,
        release,
        label
        )


def compose_hostname_opts(params):
    """Return list of hostname/domain options based on `params`.

    The domain is omitted if `params` does not include it.
    """
    options = [
        'hostname=%s' % params.hostname,
        ]
    if params.domain is not None:
        options.append('domain=%s' % params.domain)
    return options


def prefix_target_name(name):
    """Prefix an ISCSI target name with the standard target-name prefix."""
    return "%s:%s" % (ISCSI_TARGET_NAME_PREFIX, name)


def compose_purpose_opts(params):
    """Return the list of the purpose-specific kernel options."""
    if params.purpose in ["commissioning", "xinstall", "enlist"]:
        # These are kernel parameters read by the ephemeral environment.
        tname = prefix_target_name(
            get_ephemeral_name(
                params.osystem, params.arch, params.subarch,
                params.release, params.label))
        kernel_params = [
            # Read by the open-iscsi initramfs code.
            "iscsi_target_name=%s" % tname,
            "iscsi_target_ip=%s" % params.fs_host,
            "iscsi_target_port=3260",
            "iscsi_initiator=%s" % params.hostname,
            # Read by cloud-initramfs-dyn-netconf initramfs-tools networking
            # configuration in the initramfs.  Choose IPv4 or IPv6 based on the
            # family of fs_host.  If BOOTIF is set, IPv6 config uses that
            # exclusively.
            (
                "ip=::::%s:BOOTIF" % params.hostname
                if IPAddress(params.fs_host).version == 4 else "ip=off"
            ),
            (
                "ip6=dhcp"
                if IPAddress(params.fs_host).version == 6 else "ip6=off"
            ),
            # kernel / udev name iscsi devices with this path
            "ro root=/dev/disk/by-path/ip-%s:%s-iscsi-%s-lun-1" % (
                params.fs_host, "3260", tname),
            # Read by overlayroot package.
            "overlayroot=tmpfs",
            # Read by cloud-init.
            "cloud-config-url=%s" % params.preseed_url,
            ]
        return kernel_params
    else:
        # These are options used by the Debian Installer.
        return [
            "netcfg/choose_interface=auto",
            # Use the text installer, display only critical messages.
            "text priority=critical",
            compose_preseed_opt(params.preseed_url),
            compose_locale_opt(),
            ] + compose_hostname_opts(params)


def compose_arch_opts(params):
    """Return any architecture-specific options required"""
    arch_subarch = '%s/%s' % (params.arch, params.subarch)
    resource = ArchitectureRegistry.get_item(arch_subarch)
    if resource is not None and resource.kernel_options is not None:
        return resource.kernel_options
    else:
        return []


CURTIN_KERNEL_CMDLINE_NAME = 'KERNEL_CMDLINE_COPY_TO_INSTALL_SEP'


def get_curtin_kernel_cmdline_sep():
    """Return the separator for passing extra parameters to the kernel."""
    return getattr(
        curtin, CURTIN_KERNEL_CMDLINE_NAME, '--')


def compose_kernel_command_line(params):
    """Generate a line of kernel options for booting `node`.

    :type params: `KernelParameters`.
    """
    options = []
    # nomodeset prevents video mode switching.
    options += ["nomodeset"]
    options += compose_purpose_opts(params)
    # Note: logging opts are not respected by ephemeral images, so
    #       these are actually "purpose_opts" but were left generic
    #       as it would be nice to have.
    options += compose_logging_opts(params.log_host)
    options += compose_arch_opts(params)
    cmdline_sep = get_curtin_kernel_cmdline_sep()
    if params.extra_opts:
        # Using --- before extra opts makes both d-i and Curtin install
        # them into the grub config when installing an OS, thus causing
        # the options to "stick" when local booting later.
        # see LP: #1402042 for info on '---' versus '--'
        options.append(cmdline_sep)
        options.append(params.extra_opts)
    kernel_opts = ' '.join(options)
    maaslog.debug(
        '%s: kernel parameters %s "%s"' %
        (cmdline_sep, params.hostname, kernel_opts))
    return kernel_opts
