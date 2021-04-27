import os
import re
import shutil

import click
import fmf

import tmt
import tmt.beakerlib
import tmt.steps.discover

from tmt.steps.discover import DiscoverPlugin

class DiscoverSources(DiscoverPlugin):
    """
    Get sources extracted from dist-git and delegate discovery step

        discover:
            how: sources
            delegate:
                how: fmf

    Full config example:

        discover:
            how: sources
            delegate:
                how: fmf
            shell: |
                cd ./*
                fmf init
            url: https://src.fedoraproject.org/rpms/tmt.git
    """

    # Supported methods
    _methods = [tmt.steps.Method(name='sources', doc=__doc__, order=50)]

    def go(self):
        """ Prepare sources """
        super(DiscoverSources, self).go()

        sourcedir = os.path.join(self.workdir, 'TEMPORARY')
        rpmbuilddir = os.path.join(self.workdir, 'temp_tests')
        testdir = os.path.join(self.workdir, 'tests')

        url = self.get('url')

        if url:
            self.debug(f"Clone '{url}' to '{sourcedir}'.")
            self.run(
                ['git', 'clone', url, sourcedir],
                shell=False, env={"GIT_ASKPASS": "echo"})
        else:
            # Copy current directory
            self.debug(f"Copy '.' to '{sourcedir}'.")
            shutil.copytree('.', sourcedir)
        # Download sources
        self.run(
            ['fedpkg', 'sources', '--outdir', sourcedir],
            cwd=sourcedir if url else '.',
            shell=False
        )

        # Run rpmbuild
        self.run([
            "rpmbuild",
            "-bp",
            "--define",
            f"_builddir {rpmbuilddir}",
            "--define",
            f"_sourcedir {sourcedir}",
            "--define",
            f"_specdir {sourcedir}",
            "tmt.spec"],
            cwd=sourcedir,
            shell=False)

        # Move extracted sources
        shutil.move(
            os.path.join(
                rpmbuilddir,
                os.listdir(rpmbuilddir)[0]
            ),
            testdir
        )

        # Run shell if it was there
        shell = self.get('shell')
        if shell:
            self.run(shell, cwd=testdir)

        delegate = self.get('delegate', {'how','fmf'})
        delegate['name'] = 'delegated from sources'
        delegate['path'] = testdir
        plugin = DiscoverPlugin.delegate(self.step, delegate)
        # FIXME - need to manipulate copy `fmf_root = path or self.step.plan.my_run.tree.root`
        plugin.go()
        self._tests = plugin.tests()

    def tests(self):
        """ Return all discovered tests """
        return self._tests
