
""" Base Metadata Classes """

import copy
import functools
import os
import re
import shutil
import subprocess
import time

import click
import fmf
import yaml
from fmf.utils import listed
from click import echo, style

import tmt.export
import tmt.steps
import tmt.steps.discover
import tmt.steps.provision
import tmt.steps.prepare
import tmt.steps.execute
import tmt.steps.report
import tmt.steps.finish
import tmt.templates
import tmt.utils
from tmt.utils import verdict

# Default test duration is 5m for individual tests discovered from L1
# metadata and 1h for scripts defined directly in plans (L2 metadata).
DEFAULT_TEST_DURATION_L1 = '5m'
DEFAULT_TEST_DURATION_L2 = '1h'

# How many already existing lines should tmt run --follow show
FOLLOW_LINES = 10

# Obsoleted test keys
OBSOLETED_TEST_KEYS = "relevancy coverage".split()

# Unofficial temporary test keys
EXTRA_TEST_KEYS = (
    "extra-nitrate extra-hardware extra-pepa "
    "extra-summary extra-task".split())


class Node(tmt.utils.Common):
    """
    General node object

    Corresponds to given fmf.Tree node.
    Implements common Test, Plan and Story methods.
    """

    # Core attributes (supported across all levels)
    _keys = ['summary', 'description', 'enabled', 'link', 'adjust']

    def __init__(self, node, parent=None):
        """ Initialize the node """
        super(Node, self).__init__(parent=parent, name=node.name)
        self.node = node

        # Store original metadata with applied defaults and including
        # keys which are not defined in the L1 metadata specification
        # Once the whole node has been initialized,
        # self._update_metadata() must be called to work correctly.
        self._metadata = self.node.data.copy()

        # Set all core attributes
        for key in self._keys:
            setattr(self, key, self.node.get(key))

        # Check whether the node is enabled, handle the default
        self._check('enabled', expected=bool, default=True)

        # Convert link into the canonical form, store the object
        self._link = Link(self.link)
        self.link = self._link.get()

    def __str__(self):
        """ Node name """
        return self.name

    def _update_metadata(self):
        """ Update the _metadata attribute """
        self._metadata.update(self.export(format_='dict'))
        self._metadata['name'] = self.name

    def _sources(self):
        """ Show source files """
        echo(tmt.utils.format(
            'sources', self.node.sources, key_color='magenta'))

    def _check(self, key, expected, default=None, listify=False):
        """
        Check that the key is of expected type

        Handle default and convert into a list if requested.
        """
        value = getattr(self, key)
        # Handle default
        if value is None:
            setattr(self, key, default)
            return
        # Check for correct type
        if not isinstance(value, expected):
            expected = tmt.utils.listify(expected)
            expected_names = fmf.utils.listed(
                [type_.__name__ for type_ in expected], join='or')
            class_name = self.__class__.__name__.lower()
            raise tmt.utils.SpecificationError(
                f"Invalid '{key}' in {class_name} '{self.name}' (should be "
                f"a '{expected_names}', got a '{type(value).__name__}').")
        # Convert into a list if requested
        if listify:
            setattr(self, key, tmt.utils.listify(value))

    def _fmf_id(self):
        """ Show fmf identifier """
        echo(tmt.utils.format('fmf-id', self.fmf_id, key_color='magenta'))

    @property
    @functools.lru_cache(maxsize=None)
    def fmf_id(self):
        """ Return full fmf identifier of the node """

        def run(command):
            """ Run command, return output """
            result = subprocess.run(command.split(), stdout=subprocess.PIPE)
            return result.stdout.strip().decode("utf-8")

        fmf_id = {'name': self.name}

        # Prepare url (for now handle just the most common schemas)
        origin = run('git config --get remote.origin.url')
        fmf_id['url'] = tmt.utils.public_git_url(origin)

        # Get the ref (skip for master as it is the default)
        ref = run('git rev-parse --abbrev-ref HEAD')
        if ref != 'master':
            fmf_id['ref'] = ref

        # Construct path (if different from git root)
        git_root = run('git rev-parse --show-toplevel')
        fmf_root = self.node.root
        if git_root != fmf_root:
            fmf_id['path'] = os.path.join(
                '/', os.path.relpath(fmf_root, git_root))

        return fmf_id

    @classmethod
    def _save_context(cls, context):
        """ Save provided command line context for future use """
        super(Node, cls)._save_context(context)

        # Handle '.' as an alias for the current working directory
        names = cls._opt('names')
        if names is not None and '.' in names:
            root = context.obj.tree.root
            current = os.getcwd()
            # Handle special case when directly in the metadata root
            if current == root:
                path = '/'
            # Prepare path from the tree root to the current directory
            else:
                path = os.path.join('/', os.path.relpath(current, root))
            cls._context.params['names'] = (
                path if name == '.' else name for name in names)

    def name_and_summary(self):
        """ Node name and optional summary """
        if self.summary:
            return '{0} ({1})'.format(self.name, self.summary)
        else:
            return self.name

    def ls(self, summary=False):
        """ List node """
        echo(style(self.name, fg='red'))
        if summary and self.summary:
            echo(tmt.utils.format('summary', self.summary))

    def export(self, format_='yaml', keys=None):
        """ Export data into requested format (yaml or dict) """
        if keys is None:
            keys = self._keys
        data = dict([(key, getattr(self, key)) for key in keys])
        # Choose proper format
        if format_ == 'dict':
            return data
        elif format_ == 'yaml':
            return tmt.utils.dict_to_yaml(data)
        else:
            raise tmt.utils.GeneralError(
                f"Invalid test export format '{format_}'.")

    def lint_keys(self, additional_keys):
        """ Return list of invalid keys used, empty when all good """
        known_keys = additional_keys + self._keys
        return [key for key in self.node.get().keys() if key not in known_keys]


class Test(Node):
    """ Test object (L1 Metadata) """

    # Supported attributes (listed in display order)
    _keys = [
        # Basic test information
        'summary',
        'description',
        'contact',
        'component',

        # Test execution data
        'test',
        'path',
        'framework',
        'manual',
        'require',
        'recommend',
        'environment',
        'duration',
        'enabled',
        'result',

        # Filtering attributes
        'tag',
        'tier',
        'link',
        ]

    def __init__(self, data, name=None):
        """
        Initialize test data from an fmf node or a dictionary

        The following two methods are supported:

            Test(node)
            Test(dictionary, name)

        Test name is required when initializing from a dictionary.
        """

        # Create a simple test node if dictionary given
        if isinstance(data, dict):
            if name is None:
                raise tmt.utils.GeneralError(
                    'Name required to initialize test.')
            elif not name.startswith('/'):
                raise tmt.utils.SpecificationError(
                    "Test name should start with a '/'.")
            node = fmf.Tree(data)
            node.name = name
        else:
            node = data
        super().__init__(node)

        # Path defaults to the directory where metadata are stored or to
        # the root '/' if fmf metadata were not stored on the filesystem
        try:
            directory = os.path.dirname(self.node.sources[-1])
            relative_path = os.path.relpath(directory, self.node.root)
            if relative_path == '.':
                default_path = '/'
            else:
                default_path = os.path.join('/', relative_path)
        except (AttributeError, IndexError):
            default_path = '/'
        self._check('path', expected=str, default=default_path)

        # Check that lists are lists or strings, listify if needed
        for key in ['component', 'contact', 'require', 'recommend', 'tag']:
            self._check(key, expected=(list, str), default=[], listify=True)

        # FIXME Framework should default to 'shell' in the future. For
        # backward-compatibility with the old execute methods we need to be
        # able to detect if the test has explicitly set the framework.
        self._check('framework', expected=str, default=None)
        if self.framework == 'beakerlib':
            self.require.append('beakerlib')

        # Check that environment is a dictionary
        self._check('environment', expected=dict, default={})
        self.environment = dict([
            (key, str(value)) for key, value in self.environment.items()])

        # Default duration, manual, enabled and result
        self._check('duration', expected=str, default=DEFAULT_TEST_DURATION_L1)
        self._check('manual', expected=bool, default=False)
        self._check('result', expected=str, default='respect')

        self._update_metadata()

    @staticmethod
    def overview(tree):
        """ Show overview of available tests """
        tests = [
            style(str(test), fg='red') for test in tree.tests()]
        echo(style(
            'Found {}{}{}.'.format(
                listed(tests, 'test'),
                ': ' if tests else '',
                listed(tests, max=12)
                ), fg='blue'))

    @staticmethod
    def create(name, template, path, force=False, dry=None):
        """ Create a new test """
        if dry is None:
            dry = Test._opt('dry')

        # Create directory
        if name == '.':
            directory_path = os.getcwd()
        else:
            directory_path = os.path.join(path, name.lstrip('/'))
            tmt.utils.create_directory(
                directory_path, 'test directory', dry=dry)

        # Create metadata
        try:
            metadata_path = os.path.join(directory_path, 'main.fmf')
            tmt.utils.create_file(
                path=metadata_path,
                content=tmt.templates.TEST_METADATA[template],
                name='test metadata',
                dry=dry,
                force=force)
        except KeyError:
            raise tmt.utils.GeneralError(f"Invalid template '{template}'.")

        # Create script
        script_path = os.path.join(directory_path, 'test.sh')
        try:
            content = tmt.templates.TEST[template]
        except KeyError:
            raise tmt.utils.GeneralError(f"Invalid template '{template}'.")
        tmt.utils.create_file(
            path=script_path, content=content,
            name='test script', dry=dry, force=force, mode=0o755)

    def show(self):
        """ Show test details """
        self.ls()
        for key in self._keys:
            # Special handling for the link attribute
            if key == 'link':
                self._link.show()
                continue
            value = getattr(self, key)
            if value not in [None, list(), dict()]:
                echo(tmt.utils.format(key, value))
        if self.opt('verbose'):
            self._sources()
            self._fmf_id()
        if self.opt('verbose', 0) >= 2:
            # Print non-empty unofficial attributes
            for key in sorted(self.node.get().keys()):
                # Already asked to be printed
                if key in self._keys:
                    continue
                value = self.node.get(key)
                if value not in [None, list(), dict()]:
                    echo(tmt.utils.format(key, value, key_color='blue'))

    def lint(self):
        """
        Check test against the L1 metadata specification.

        Return whether the test is valid.
        """
        self.ls()
        stripped_path = self.path.strip()
        test_path = self.node.root + stripped_path

        # Check test, path and summary (use bitwise '&' because 'and' is
        # lazy and would skip all verdicts following the first fail)
        valid = verdict(
            bool(self.test), 'test script must be defined')
        valid &= verdict(
            stripped_path.startswith('/'), 'directory path must be absolute')
        valid &= verdict(
            os.path.exists(test_path), 'directory path must exist')
        if self.summary is None:
            verdict(None, 'summary is very useful for quick inspection')
        elif len(self.summary) > 50:
            verdict(None, 'summary should not exceed 50 characters')

        # Check for possible test case relevancy rules
        filename = self.node.sources[-1]
        metadata = tmt.utils.yaml_to_dict(self.read(filename))
        relevancy = metadata.pop('relevancy', None)
        if relevancy:
            # Convert into adjust rules if --fix enabled
            if self.opt('fix'):
                metadata['adjust'] = tmt.convert.relevancy_to_adjust(relevancy)
                self.write(filename, tmt.utils.dict_to_yaml(metadata))
                verdict(None, 'relevancy converted into adjust')
            else:
                valid = verdict(
                    False, 'relevancy has been obsoleted by adjust')

        # Check for possible coverage attribute
        coverage = metadata.pop('coverage', None)
        if coverage:
            valid = verdict(False, 'coverage has been obsoleted by link')
        # Check for unknown attributes
        # FIXME - Make additional attributes configurable
        # We don't want adjust in show/export so it is not yet in Test._keys
        invalid_keys = self.lint_keys(
            EXTRA_TEST_KEYS + OBSOLETED_TEST_KEYS + ['adjust'])
        if invalid_keys:
            valid = False
            for key in invalid_keys:
                verdict(False, f"unknown attribute '{key}' is used")
        else:
            verdict(True, "correct attributes are used")
        return valid

    def export(self, format_='yaml', keys=None):
        """
        Export test data into requested format

        In addition to 'yaml' and 'dict' it supports also a special
        format 'execute' used by the execute step which returns
        (test-name, test-data) tuples.
        """

        # Prepare special format for the executor
        if format_ == 'execute':
            name = self.name
            data = dict()
            data['test'] = self.test
            data['path'] = self.path
            data['framework'] = self.framework
            if self.duration is not None:
                data['duration'] = self.duration
            if self.environment:
                data['environment'] = ' '.join(
                    tmt.utils.shell_variables(self.environment))
            return data

        # Export to Nitrate test case management system
        elif format_ == 'nitrate':
            tmt.export.export_to_nitrate(self)

        # Common node export otherwise
        else:
            return super(Test, self).export(format_, keys)


class Plan(Node):
    """ Plan object (L2 Metadata) """

    def __init__(self, node, run=None):
        """ Initialize the plan """
        super().__init__(node, parent=run)
        self.my_run = run

        # Initialize test steps
        self.discover = tmt.steps.discover.Discover(
            self.node.get('discover'), self)
        self.provision = tmt.steps.provision.Provision(
            self.node.get('provision'), self)
        self.prepare = tmt.steps.prepare.Prepare(
            self.node.get('prepare'), self)
        self.execute = tmt.steps.execute.Execute(
            self.node.get('execute'), self)
        self.report = tmt.steps.report.Report(
            self.node.get('report'), self)
        self.finish = tmt.steps.finish.Finish(
            self.node.get('finish'), self)

        # Initialize workdir tree
        self._initialize_worktree()

        # Relevant gates (convert to list if needed)
        self.gate = node.get('gate')
        if self.gate:
            if not isinstance(self.gate, list):
                self.gate = [self.gate]

        # Environment variables, make sure that values are string
        self._environment = dict([
            (key, str(value)) for key, value
            in node.get('environment', dict()).items()])

        # Test execution context defined in the plan
        self._plan_context = self.node.get('context', dict())

        self._update_metadata()

    @property
    def environment(self):
        """ Return combined environment from plan data and command line """
        if self.my_run and self.my_run.environment:
            combined = self._environment.copy()
            combined.update(self.my_run.environment)
            return combined
        else:
            return self._environment

    def _initialize_worktree(self):
        """
        Prepare the worktree, a copy of the metadata tree root

        Used as cwd in prepare, execute and finish steps.
        """
        # Bail out if workdir does not exist (not running the plan)
        if not self.workdir:
            return

        self.worktree = os.path.join(self.workdir, 'tree')
        tree_root = self.my_run.tree.root

        # Create an empty directory if there's no metadata tree
        if not tree_root:
            self.debug('Create an empty worktree (no metadata tree).')
            os.makedirs(self.worktree, exist_ok=True)
            return

        # Sync metadata root to the worktree
        self.debug(f"Sync the worktree to '{self.worktree}'.")
        self.run(f'rsync -ar --exclude .git {tree_root}/ {self.worktree}')

    def _fmf_context(self):
        """ Return combined context from plan data and command line """
        combined = self._plan_context.copy()
        combined.update(self._context.obj.fmf_context)
        return combined

    @staticmethod
    def edit_template(content):
        """ Edit the default template with custom values """

        content = tmt.utils.yaml_to_dict(content)

        # For each step check for possible command line data
        for step in tmt.steps.STEPS:
            options = Plan._opt(step)
            if not options:
                continue
            step_data = []

            # For each option check for valid yaml and store
            for option in options:
                try:
                    data = tmt.utils.yaml_to_dict(option)
                    if not data:
                        raise tmt.utils.GeneralError(
                            f"Invalid step data for {step}: '{data}'.")
                    step_data.append(data)
                except yaml.parser.ParserError as error:
                    raise tmt.utils.GeneralError(
                        f"Invalid yaml data for {step}:\n{error}")

            # Use list only when multiple step data provided
            if len(step_data) == 1:
                step_data = step_data[0]
            content[step] = step_data

        return tmt.utils.dict_to_yaml(content)

    @staticmethod
    def overview(tree):
        """ Show overview of available plans """
        plans = [
            style(str(plan), fg='red') for plan in tree.plans()]
        echo(style(
            'Found {}{}{}.'.format(
                listed(plans, 'plan'),
                ': ' if plans else '',
                listed(plans, max=12)
                ), fg='blue'))

    @staticmethod
    def create(name, template, path, force=False, dry=None):
        """ Create a new plan """
        # Prepare paths
        if dry is None:
            dry = Plan._opt('dry')

        (directory, plan) = os.path.split(name)
        directory_path = os.path.join(path, directory.lstrip('/'))
        has_fmf_ext = os.path.splitext(plan)[1] == '.fmf'
        plan_path = os.path.join(
            directory_path, plan + ('' if has_fmf_ext else '.fmf'))

        # Create directory & plan
        tmt.utils.create_directory(directory_path, 'plan directory', dry=dry)
        try:
            content = tmt.templates.PLAN[template]
        except KeyError:
            raise tmt.utils.GeneralError(
                "Invalid template '{}'.".format(template))

        # Override template with data provided on command line
        content = Plan.edit_template(content)

        tmt.utils.create_file(
            path=plan_path, content=content,
            name='plan', dry=dry, force=force)

    def steps(self, enabled=True, disabled=False, names=False, skip=None):
        """
        Iterate over enabled / all steps

        Yields instances of all enabled steps by default. Use 'names' to
        yield step names only and 'disabled=True' to iterate over all.
        Use 'skip' to pass the list of steps to be skipped.
        """
        skip = skip or []
        for name in tmt.steps.STEPS:
            if name in skip:
                continue
            step = getattr(self, name)
            if (enabled and step.enabled or disabled and not step.enabled):
                yield name if names else step

    def show(self):
        """ Show plan details """
        self.ls(summary=True)
        if self.description:
            echo(tmt.utils.format(
                'description', self.description, key_color='green'))
        for step in self.steps(disabled=True):
            step.show()
        if self.environment:
            echo(tmt.utils.format(
                'environment', self.environment, key_color='blue'))
        echo(tmt.utils.format('enabled', self.enabled, key_color='cyan'))
        self._link.show()
        if self._fmf_context():
            echo(tmt.utils.format(
                'context', self._fmf_context(), key_color='blue'))
        if self.opt('verbose'):
            self._sources()

    def _lint_execute(self):
        """ Lint execute step """
        execute = self.node.get('execute')
        if not execute:
            return verdict(False, "execute step must be defined with 'how'")
        if isinstance(execute, dict):
            execute = [execute]

        methods = [
            method.name
            for method in tmt.steps.execute.ExecutePlugin.methods()]
        correct = True
        for configuration in execute:
            how = configuration.get('how')
            if how not in methods:
                name = configuration.get('name')
                verdict(False,
                        f"unsupported execute method '{how}' in '{name}'")
                correct = False

        return correct

    def _lint_summary(self):
        """ Lint summary step """
        # Summary is advised with a resonable length
        if self.summary is None:
            verdict(None, 'summary is very useful for quick inspection')
        elif len(self.summary) > 50:
            verdict(None, 'summary should not exceed 50 characters')

        return True

    def _lint_discover(self):
        """ Lint discover step """
        # The discover step is optional
        discover = self.node.get('discover')
        if not discover:
            return True
        if isinstance(discover, dict):
            discover = [discover]

        methods = [
            method.name
            for method in tmt.steps.discover.DiscoverPlugin.methods()]
        correct = True
        for configuration in discover:
            how = configuration.get('how')

            if how not in methods:
                verdict(False, f"unknown discover method '{how}'")
                correct = False
                continue

            # FIXME Add check for the shell discover method
            if how == 'shell':
                continue

            if not self._lint_discover_fmf(configuration):
                correct = False
        return correct

    @staticmethod
    def _lint_discover_fmf(discover):
        """ Lint fmf discover method """
        # Validate remote id and translate to human readable errors
        valid, error = tmt.utils.validate_fmf_id(
            {key: value for key, value in discover.items()
                if key in ['url', 'ref', 'path']})

        if valid:
            name = discover.get('name')
            return verdict(True, f"fmf remote id in '{name}' is valid")

        return verdict(False, error)

    def lint(self):
        """
        Check plan against the L2 metadata specification

        Return whether the plan is valid.
        """
        self.ls()

        # Explore all available plugins
        tmt.plugins.explore()

        return all([
            self._lint_summary(),
            self._lint_execute(),
            self._lint_discover()])

    def go(self):
        """ Execute the plan """
        # Show plan name and summary (one blank line to separate plans)
        self.info('')
        self.info(self.name, color='red')
        if self.summary:
            self.verbose('summary', self.summary, 'green')

        # Additional debug info like plan environment
        self.debug('info', color='cyan', shift=0, level=3)
        self.debug('environment', self.environment, 'magenta', level=3)
        self.debug('context', self._fmf_context(), 'magenta', level=3)

        # Wake up all steps
        self.debug('wake', color='cyan', shift=0, level=2)
        for step in self.steps(disabled=True):
            self.debug(str(step), color='blue', level=2)
            try:
                step.wake()
            except tmt.utils.SpecificationError as error:
                # Re-raise the exception if the step is enabled (invalid
                # step data), otherwise just warn the user and continue.
                if step.enabled:
                    raise error
                else:
                    step.warn(error)

        # Run enabled steps except 'finish'
        self.debug('go', color='cyan', shift=0, level=2)
        try:
            abort = False
            for step in self.steps(skip=['finish']):
                step.go()
                # Finish plan if no tests found (except dry mode)
                if (step.name == 'discover' and not step.tests()
                        and not self.opt('dry')):
                    step.info(
                        'warning', 'No tests found, finishing plan.',
                        color='yellow', shift=1)
                    abort = True
                    return
        # Make sure we run 'finish' step always if enabled
        finally:
            if not abort and self.finish.enabled:
                self.finish.go()


class Story(Node):
    """ User story object """

    # Supported attributes (listed in display order)
    _keys = [
        'summary',
        'title',
        'story',
        'description',
        'example',
        'enabled',
        'link',
        ]

    def __init__(self, node):
        """ Initialize the story """
        super().__init__(node)
        self._update_metadata()

    @property
    def documented(self):
        """ Return links to relevant documentation """
        return self._link.get('documented-by')

    @property
    def verified(self):
        """ Return links to relevant test coverage """
        return self._link.get('verified-by')

    @property
    def implemented(self):
        """ Return links to relevant source code """
        return self._link.get('implemented-by')

    def _match(
            self, implemented, verified, documented, covered,
            unimplemented, unverified, undocumented, uncovered):
        """ Return true if story matches given conditions """
        if implemented and not self.implemented:
            return False
        if verified and not self.verified:
            return False
        if documented and not self.documented:
            return False
        if unimplemented and self.implemented:
            return False
        if unverified and self.verified:
            return False
        if undocumented and self.documented:
            return False
        if uncovered and (
                self.implemented and self.verified and self.documented):
            return False
        if covered and not (
                self.implemented and self.verified and self.documented):
            return False
        return True

    @staticmethod
    def create(name, template, path, force=False, dry=None):
        """ Create a new story """
        if dry is None:
            dry = Story._opt('dry')

        # Prepare paths
        (directory, story) = os.path.split(name)
        directory_path = os.path.join(path, directory.lstrip('/'))
        has_fmf_ext = os.path.splitext(story)[1] == '.fmf'
        story_path = os.path.join(directory_path,
                                  story + ('' if has_fmf_ext else '.fmf'))

        # Create directory & story
        tmt.utils.create_directory(directory_path, 'story directory', dry=dry)
        try:
            content = tmt.templates.STORY[template]
        except KeyError:
            raise tmt.utils.GeneralError(
                "Invalid template '{}'.".format(template))

        tmt.utils.create_file(
            path=story_path, content=content,
            name='story', dry=dry, force=force)

    @staticmethod
    def overview(tree):
        """ Show overview of available stories """
        stories = [
            style(str(story), fg='red') for story in tree.stories()]
        echo(style(
            'Found {}{}{}.'.format(
                listed(stories, 'story'),
                ': ' if stories else '',
                listed(stories, max=12)
                ), fg='blue'))

    def show(self):
        """ Show story details """
        self.ls()
        for key in self._keys:
            if key == 'link':
                self._link.show()
                continue
            value = getattr(self, key)
            if value is not None:
                wrap = False if key == 'example' else 'auto'
                echo(tmt.utils.format(key, value, wrap=wrap))
        if self.opt('verbose'):
            self._sources()

    def coverage(self, code, test, docs):
        """ Show story coverage """
        if code:
            code = bool(self.implemented)
            verdict(code, good='done ', bad='todo ', nl=False)
        if test:
            test = bool(self.verified)
            verdict(test, good='done ', bad='todo ', nl=False)
        if docs:
            docs = bool(self.documented)
            verdict(docs, good='done ', bad='todo ', nl=False)
        echo(self)
        return (code, test, docs)

    def export(self, format_='rst', title=True):
        """ Export story data into requested format """

        # Use common Node export unless 'rst' requested
        if format_ != 'rst':
            return super().export(format_=format_)

        output = ''

        # Title and its anchor
        if title:
            depth = len(re.findall('/', self.name)) - 1
            if self.title and self.title != self.node.parent.get('title'):
                title = self.title
            else:
                title = re.sub('.*/', '', self.name)
            output += f'\n.. _{self.name}:\n'
            output += '\n{}\n{}\n'.format(title, '=~^:-><'[depth] * len(title))

        # Summary, story and description
        if self.summary and self.summary != self.node.parent.get('summary'):
            output += '\n{}\n'.format(self.summary)
        if self.story != self.node.parent.get('story'):
            output += '\n*{}*\n'.format(self.story.strip())
        # Insert note about unimplemented feature (leaf nodes only)
        if not self.node.children and not self.implemented:
            output += '\n.. note:: This is a draft, '
            output += 'the story is not implemented yet.\n'
        if (self.description and
                self.description != self.node.parent.get('description')):
            output += '\n{}\n'.format(self.description)

        # Examples
        if self.example and self.example != self.node.parent.get('example'):
            output += '\nExamples::\n\n'
            output += tmt.utils.format(
                '', self.example, wrap=False, indent=4,
                key_color=None, value_color=None) + '\n'

        # Status
        if not self.node.children:
            status = []
            for coverage in ['implemented', 'verified', 'documented']:
                if getattr(self, coverage):
                    status.append(coverage)
            output += "\nStatus: {}\n".format(
                listed(status) if status else 'idea')

        return output


class Tree(tmt.utils.Common):
    """ Test Metadata Tree """

    def __init__(self, path='.', tree=None, context=None):
        """ Initialize tmt tree from directory path or given fmf tree """
        self._path = path
        self._tree = tree
        self._custom_context = context

    def _fmf_context(self):
        """ Use custom fmf context if provided, default otherwise """
        if self._custom_context is not None:
            return self._custom_context
        return super()._fmf_context()

    def _filters_conditions(self, nodes, filters, conditions):
        """ Apply filters and conditions, return pruned nodes """
        result = []
        for node in nodes:
            filter_vars = copy.deepcopy(node._metadata)
            cond_vars = node._metadata
            # Add a lowercase version of bool variables for filtering
            bool_vars = {
                key: [value, str(value).lower()]
                for key, value in filter_vars.items()
                if isinstance(value, bool)}
            filter_vars.update(bool_vars)
            # Conditions
            try:
                if not all([fmf.utils.evaluate(condition, cond_vars, node)
                            for condition in conditions]):
                    continue
            except fmf.utils.FilterError as error:
                # Handle missing attributes as if condition failed
                continue
            except Exception as error:
                raise tmt.utils.GeneralError(
                    f"Invalid --condition raised exception: {error}")
            # Filters
            try:
                if not all([fmf.utils.filter(filter_, filter_vars, regexp=True)
                            for filter_ in filters]):
                    continue
            except fmf.utils.FilterError as error:
                # Handle missing attributes as if filter failed
                continue
            result.append(node)
        return result

    @property
    def tree(self):
        """ Initialize tree only when accessed """
        if self._tree is None:
            try:
                self._tree = fmf.Tree(self._path)
            except fmf.utils.RootError:
                raise tmt.utils.MetadataError(
                    f"No metadata found in the '{self._path}' directory. "
                    f"Use 'tmt init' to get started.")
            except fmf.utils.FileError as error:
                raise tmt.utils.GeneralError(f"Invalid yaml syntax: {error}")
            # Adjust metadata for current fmf context
            self._tree.adjust(fmf.context.Context(**self._fmf_context()))
        return self._tree

    @property
    def root(self):
        """ Metadata root """
        return self.tree.root

    def tests(self, keys=None, names=None, filters=None, conditions=None):
        """ Search available tests """
        # Handle defaults, apply possible command line options
        keys = (keys or []) + ['test']
        names = (names or []) + list(Test._opt('names', []))
        filters = (filters or []) + list(Test._opt('filters', []))
        conditions = (conditions or []) + list(Test._opt('conditions', []))

        # Build the list and convert to objects
        return self._filters_conditions(
            [Test(test) for test in self.tree.prune(keys=keys, names=names)],
            filters, conditions)

    def plans(self, keys=None, names=None, filters=None, conditions=None,
              run=None):
        """ Search available plans """
        # Handle defaults, apply possible command line options
        keys = (keys or []) + ['execute']
        names = (names or []) + list(Plan._opt('names', []))
        filters = (filters or []) + list(Plan._opt('filters', []))
        conditions = (conditions or []) + list(Plan._opt('conditions', []))

        # Build the list and convert to objects
        return self._filters_conditions(
            [Plan(plan, run=run)
                for plan in self.tree.prune(keys=keys, names=names)],
            filters, conditions)

    def stories(self, keys=None, names=None, filters=None, conditions=None,
                whole=False):
        """ Search available stories """
        # Handle defaults, apply possible command line options
        keys = (keys or []) + ['story']
        names = (names or []) + list(Story._opt('names', []))
        filters = (filters or []) + list(Story._opt('filters', []))
        conditions = (conditions or []) + list(Story._opt('conditions', []))

        # Build the list and convert to objects
        return self._filters_conditions(
            [Story(story) for story in self.tree.prune(
                keys=keys, names=names, whole=whole)],
            filters, conditions)

    @staticmethod
    def init(path, template, force, **kwargs):
        """ Initialize a new tmt tree, optionally with a template """
        path = os.path.realpath(path)
        dry = Tree._opt('dry')

        # Check for existing tree
        try:
            tree = tmt.Tree(path)
            # Are we creating a new tree under the existing one?
            if path == tree.root:
                echo("Tree '{}' already exists.".format(tree.root))
            else:
                tree = None
        except tmt.utils.GeneralError:
            tree = None

        # Create a new tree
        if tree is None:
            if dry:
                echo(f"Tree '{path}' would be initialized.")
            else:
                try:
                    fmf.Tree.init(path)
                    tree = tmt.Tree(path)
                    path = tree.root
                except fmf.utils.GeneralError as error:
                    raise tmt.utils.GeneralError(
                        f"Failed to initialize tree in '{path}': {error}")
                echo(f"Tree '{tree.root}' initialized.")

        # Populate the tree with example objects if requested
        if template == 'empty':
            choices = listed(tmt.templates.INIT_TEMPLATES, join='or')
            echo(
                f"To populate it with example content, "
                f"use --template with {choices}.")
        else:
            echo(f"Applying template '{template}'.")

        if template == 'mini':
            tmt.Plan.create('/plans/example', 'mini', path, force, dry)
        elif template == 'base':
            tmt.Test.create('/tests/example', 'beakerlib', path, force, dry)
            tmt.Plan.create('/plans/example', 'base', path, force, dry)
        elif template == 'full':
            tmt.Test.create('/tests/example', 'shell', path, force, dry)
            tmt.Plan.create('/plans/example', 'full', path, force, dry)
            tmt.Story.create('/stories/example', 'full', path, force, dry)


class Run(tmt.utils.Common):
    """ Test run, a container of plans """

    def __init__(self, id_=None, tree=None, context=None):
        """ Initialize tree, workdir and plans """
        # Use the last run id if requested
        self.config = tmt.utils.Config()
        if context.params.get('last'):
            id_ = self.config.last_run()
            if id_ is None:
                raise tmt.utils.GeneralError(
                    "No last run id found. Have you executed any run?")
        if context.params.get('follow') and id_ is None:
            raise tmt.utils.GeneralError(
                "Run id has to be specified in order to use --follow.")
        # Do not create workdir now, postpone it until later, as options
        # have not been processed yet and we do not want commands such as
        # tmt run discover --how fmf --help to create a new workdir.
        super().__init__(context=context)
        self._workdir_path = id_ or True
        self._save_tree(tree)
        self._plans = None
        self._environment = dict()
        self.remove = self.opt('remove')

    def _use_default_plan(self):
        """ Prepare metadata tree with only the default plan """
        default_plan = tmt.utils.yaml_to_dict(tmt.templates.DEFAULT_PLAN)
        # The default discover method for this case is 'shell'
        default_plan['/plans/default']['discover']['how'] = 'shell'
        self.tree = tmt.Tree(tree=fmf.Tree(default_plan))
        self.debug(f"No metadata found, using the default plan.")

    def _save_tree(self, tree):
        """ Save metadata tree, handle the default plan """
        default_plan = tmt.utils.yaml_to_dict(tmt.templates.DEFAULT_PLAN)
        try:
            self.tree = tree if tree else tmt.Tree('.')
            self.debug(f"Using tree '{self.tree.root}'.")
            # Insert default plan if no plan detected
            if not list(self.tree.tree.prune(keys=['execute'])):
                self.tree.tree.update(default_plan)
                self.debug(f"No plan found, adding the default plan.")
        # Create an empty default plan if no fmf metadata found
        except tmt.utils.MetadataError:
            self._use_default_plan()

    @property
    def environment(self):
        """ Return environment combined from wake up and command line """
        combined = self._environment.copy()
        combined.update(tmt.utils.environment_to_dict(self.opt('environment')))
        return combined

    def save(self):
        """ Save list of selected plans and enabled steps """
        data = {
            'root': self.tree.root,
            'plans': [plan.name for plan in self._plans],
            'steps': list(self._context.obj.steps),
            'environment': self.environment,
            'remove': self.remove,
            }
        self.write('run.yaml', tmt.utils.dict_to_yaml(data))

    def load_from_workdir(self):
        """
        Load the run from its workdir, do not require the root in
        run.yaml to exist. Doest not load the fmf tree.

        Use only when the data in workdir is sufficient (e.g. tmt
        clean and status only require the steps to be loaded and
        their status).
        """
        self._workdir_load(self._workdir_path)
        try:
            data = tmt.utils.yaml_to_dict(self.read('run.yaml'))
        except tmt.utils.FileError:
            self.debug('Run data not found.')
            return
        self._environment = data.get('environment')
        self._context.obj.steps = set(data['steps'])
        plans = []
        # The root directory of the tree may not be available, create
        # a partial Node object that only contains the necessary
        # attributes required for plan/step loading.
        for plan in data.get('plans'):
            node = type('Node', (), {
                'name': plan,
                'data': {},
                # No attributes will ever need to be accessed, just create
                # a compatible method signature
                'get': lambda section, item=None: item,
                })
            plans.append(Plan(node, run=self))

        self._plans = plans

    def load(self):
        """ Load list of selected plans and enabled steps """
        try:
            data = tmt.utils.yaml_to_dict(self.read('run.yaml'))
        except tmt.utils.FileError:
            self.debug('Run data not found.')
            return

        # If run id was given and root was not explicitly specified,
        # create a new Tree from the root in run.yaml
        if self._workdir and 'root' in data and not self.opt('root'):
            if data['root']:
                self._save_tree(tmt.Tree(data['root']))
            else:
                # The run was used without any metadata, default plan
                # was used, load it
                self._use_default_plan()

        # Filter plans by name unless specified on the command line
        plan_options = ['names', 'filters', 'conditions']
        if not any([Plan._opt(option) for option in plan_options]):
            self._plans = [
                plan for plan in self.tree.plans(run=self)
                if plan.name in data['plans']]

        # Initialize steps only if not selected on the command line
        step_options = 'all since until after before skip'.split()
        selected = any([self.opt(option) for option in step_options])
        if not selected and not self._context.obj.steps:
            self._context.obj.steps = set(data['steps'])

        # Store loaded environment
        self._environment = data.get('environment')
        self.debug(f"Loaded environment: '{self._environment}'.", level=3)

        # If the remove was enabled, restore it, option overrides
        self.remove = self.remove or data.get('remove', 'False')
        self.debug(f"Remove workdir when finished: {self.remove}", level=3)

    @property
    def plans(self):
        """ Test plans for execution """
        if self._plans is None:
            self._plans = self.tree.plans(run=self, filters=['enabled:true'])
        return self._plans

    def finish(self):
        """ Check overall results, return appropriate exit code """
        # We get interesting results only if execute or prepare step is enabled
        execute = self.plans[0].execute
        report = self.plans[0].report
        interesting_results = execute.enabled or report.enabled

        # Gather all results and give an overall summary
        results = [
            result
            for plan in self.plans
            for result in plan.execute.results()]
        if interesting_results:
            self.info('')
            self.info('total', Result.summary(results), color='cyan')

        # Remove the workdir if enabled
        if self.remove and self.plans[0].finish.enabled:
            self._workdir_cleanup(self.workdir)

        # Skip handling of the exit codes in dry mode and
        # when there are no interesting results available
        if self.opt('dry') or not interesting_results:
            return

        # Return appropriate exit code based on the total stats
        stats = Result.total(results)
        if sum(stats.values()) == 0:
            raise SystemExit(3)
        if stats['error']:
            raise SystemExit(2)
        if stats['fail'] + stats['warn']:
            raise SystemExit(1)
        if stats['pass']:
            raise SystemExit(0)
        raise SystemExit(2)

    def follow(self):
        """ Periodically check for new lines in the log. """
        logfile = open(os.path.join(self.workdir, tmt.utils.LOG_FILENAME), 'r')
        # Move to the end of the file
        logfile.seek(0, os.SEEK_END)
        # Rewind some lines back to show more context
        location = logfile.tell()
        read_lines = 0
        while location >= 0:
            logfile.seek(location)
            location -= 1
            current_char = logfile.read(1)
            if current_char == '\n':
                read_lines += 1
            if read_lines > FOLLOW_LINES:
                break

        while True:
            line = logfile.readline()
            if line:
                print(line, end='')
            else:
                time.sleep(0.5)

    def go(self):
        """ Go and do test steps for selected plans """
        # Create the workdir and save last run
        self._workdir_load(self._workdir_path)
        self.config.last_run(self.workdir)
        # Show run id / workdir path
        self.info(self.workdir, color='magenta')
        self.debug(f"tmt version: {tmt.__version__}")
        # Attempt to load run data
        self.load()

        if self.opt('follow'):
            self.follow()

        # Enable selected steps
        enabled_steps = self._context.obj.steps
        all_steps = self.opt('all') or not enabled_steps
        since = self.opt('since')
        until = self.opt('until')
        after = self.opt('after')
        before = self.opt('before')
        skip = self.opt('skip')

        if any([all_steps, since, until, after, before]):
            # Detect index of the first and last enabled step
            if since:
                first = tmt.steps.STEPS.index(since)
            elif after:
                first = tmt.steps.STEPS.index(after) + 1
            else:
                first = tmt.steps.STEPS.index('discover')
            if until:
                last = tmt.steps.STEPS.index(until)
            elif before:
                last = tmt.steps.STEPS.index(before) - 1
            else:
                last = tmt.steps.STEPS.index('finish')
            # Enable all steps between the first and last
            for index in range(first, last + 1):
                step = tmt.steps.STEPS[index]
                if step not in skip:
                    enabled_steps.add(step)
        self.debug(f"Enabled steps: {fmf.utils.listed(enabled_steps)}")

        # Show summary, store run data
        if not self.plans:
            raise tmt.utils.GeneralError("No plans found.")
        self.verbose('Found {0}.'.format(listed(self.plans, 'plan')))
        self.save()

        # Iterate over plans
        for plan in self.plans:
            plan.go()

        # Update the last run id at the very end
        # (override possible runs created during execution)
        self.config.last_run(self.workdir)

        # Give the final summary, remove workdir, handle exit codes
        self.finish()


class Status(tmt.utils.Common):
    """ Status of tmt work directories. """

    LONGEST_STEP = max(tmt.steps.STEPS, key=lambda k: len(k))
    FIRST_COL_LEN = len(LONGEST_STEP) + 2

    @staticmethod
    def get_overall_plan_status(plan):
        """ Examines the plan status (find the last done step) """
        steps = list(plan.steps())
        step_names = list(plan.steps(names=True))
        for i in range(len(steps) - 1, -1, -1):
            if steps[i].status() == 'done':
                if i + 1 == len(steps):
                    # Last enabled step, consider the whole plan done
                    return 'done'
                else:
                    return step_names[i]
        return 'todo'

    def plan_matches_filters(self, plan):
        """ Check if the given plan matches filters from the command line """
        if self.opt('abandoned'):
            return (plan.provision.status() == 'done'
                    and plan.finish.status() == 'todo')
        if self.opt('active'):
            return any(step.status() == 'todo' for step in plan.steps())
        if self.opt('finished'):
            return all(step.status() == 'done' for step in plan.steps())
        return True

    @staticmethod
    def colorize_column(content):
        """ Add color to a status column """
        if 'done' in content:
            return style(content, fg='green')
        else:
            return style(content, fg='yellow')

    @classmethod
    def pad_with_spaces(cls, string):
        """ Append spaces to string to properly align the first column """
        return string + (cls.FIRST_COL_LEN - len(string)) * ' '

    def run_matches_filters(self, run):
        """ Check if the given run matches filters from the command line """
        if self.opt('abandoned') or self.opt('active'):
            # Any of the plans must be abandoned/active for the whole
            # run to be abandoned/active
            return any(self.plan_matches_filters(p) for p in run.plans)
        if self.opt('finished'):
            # All plans must be finished for the whole run to be finished
            return all(self.plan_matches_filters(p) for p in run.plans)
        return True

    def print_run_status(self, run):
        """ Display the overall status of the run """
        if not self.run_matches_filters(run):
            return
        # Find the earliest step in all plans' status
        earliest_step_index = len(tmt.steps.STEPS)
        for plan in run.plans:
            plan_status = self.get_overall_plan_status(plan)
            if plan_status == 'done':
                continue
            elif plan_status == 'todo':
                # If plan has no steps done, consider the whole run not done
                earliest_step_index = -1
                break
            plan_status_index = tmt.steps.STEPS.index(plan_status)
            if plan_status_index < earliest_step_index:
                earliest_step_index = plan_status_index

        if earliest_step_index == len(tmt.steps.STEPS):
            run_status = 'done'
        elif earliest_step_index == -1:
            run_status = 'todo'
        else:
            run_status = tmt.steps.STEPS[earliest_step_index]
        run_status = self.colorize_column(self.pad_with_spaces(run_status))
        echo(run_status, nl=False)
        echo(run.workdir)

    def print_plans_status(self, run):
        """ Display the status of each plan of the given run """
        for plan in run.plans:
            if self.plan_matches_filters(plan):
                plan_status = self.get_overall_plan_status(plan)
                echo(self.colorize_column(self.pad_with_spaces(plan_status)),
                     nl=False)
                echo(f'{run.workdir}  {plan.name}')

    def print_verbose_status(self, run):
        """ Display the status of each step of the given run """
        for plan in run.plans:
            if self.plan_matches_filters(plan):
                for step in plan.steps(disabled=True):
                    column = (step.status() or '----') + ' '
                    echo(self.colorize_column(column), nl=False)
                echo(f' {run.workdir}  {plan.name}')

    def process_run(self, run):
        """ Display the status of the given run based on verbosity """
        loaded, error = tmt.utils.load_run(run)
        if not loaded:
            self.warn(f"Failed to load run '{run.workdir}': {error}")
            return
        if self.opt('verbose') == 0:
            self.print_run_status(run)
        elif self.opt('verbose') == 1:
            self.print_plans_status(run)
        else:
            self.print_verbose_status(run)

    def print_header(self):
        """ Print the header of the status table based on verbosity """
        header = ''
        if self.opt('verbose') >= 2:
            for step in tmt.steps.STEPS:
                header += (step[0:4] + ' ')
            header += ' '
        else:
            header = self.pad_with_spaces('status')
        header += 'id'
        echo(style(header, fg='blue'))

    def show(self):
        """ Display the current status """
        # Prepare absolute workdir path if --id was used
        id_ = self.opt('id')
        path = self.opt('path')
        self.print_header()
        for abs_path in tmt.utils.generate_runs(path, id_):
            run = Run(abs_path, self._context.obj.tree, self._context)
            self.process_run(run)


class Clean(tmt.utils.Common):
    """ A class for cleaning up workdirs, guests or images """

    def images(self):
        """ Clean images of provision plugins """
        self.info('images', color='blue')
        for method in tmt.steps.provision.ProvisionPlugin.methods():
            method.class_.clean_images(self, self.opt('dry'))

    def _matches_how(self, plan):
        """ Check if the given plan matches options """
        how = plan.provision.data[0]['how']
        target_how = self.opt('how')
        if target_how:
            return how == target_how
        # No target provision method, always matches
        return True

    def _stop_running_guests(self, run):
        """ Stop all running guests of a run """
        loaded, error = tmt.utils.load_run(run)
        if not loaded:
            self.warn(f"Failed to load run '{run.workdir}': {error}")
            return False
        # Clean guests if provision is done but finish is not done
        successful = True
        for plan in run.plans:
            if plan.provision.status() == 'done':
                if plan.finish.status() != 'done':
                    # Wake up provision to load the active guests
                    plan.provision.wake()
                    if not self._matches_how(plan):
                        continue
                    if self.opt('dry'):
                        self.verbose(
                            f"Would stop guests in run '{run.workdir}'"
                            f" plan '{plan.name}'.", shift=1)
                    else:
                        self.verbose(f"Stopping guests in run '{run.workdir}' "
                                     f"plan '{plan.name}'.", shift=1)
                        # Set --quiet to avoid finish logging to terminal
                        quiet = self._context.params['quiet']
                        self._context.params['quiet'] = True
                        try:
                            plan.finish.go()
                        except tmt.utils.GeneralError as error:
                            self.warn(f"Could not stop guest in run "
                                      f"'{run.workdir}': {error}.", shift=1)
                            successful = False
                        finally:
                            self._context.params['quiet'] = quiet
        return successful

    def guests(self):
        """ Clean guests of runs """
        self.info('guests', color='blue')
        path = self.opt('path')
        id_ = self.opt('id_')
        if self.opt('last'):
            # Pass the context containing --last to Run to choose
            # the correct one.
            return self._stop_running_guests(Run(context=self._context))
        successful = True
        for abs_path in tmt.utils.generate_runs(path, id_):
            run = Run(abs_path, self._context.obj.tree, self._context)
            if not self._stop_running_guests(run):
                successful = False
        return successful

    def _clean_workdir(self, path):
        """ Remove a workdir (unless in dry mode) """
        if self.opt('dry'):
            self.verbose(f"Would remove workdir '{path}'.", shift=1)
        else:
            self.verbose(f"Removing workdir '{path}'.", shift=1)
            try:
                shutil.rmtree(path)
            except OSError as error:
                self.warn(f"Failed to remove '{path}': {error}.", shift=1)
                return False
        return True

    def runs(self):
        """ Clean workdirs of runs """
        self.info('runs', color='blue')
        path = self.opt('path')
        id_ = self.opt('id_')
        if self.opt('last'):
            # Pass the context containing --last to Run to choose
            # the correct one.
            last_run = Run(context=self._context)
            last_run._workdir_load(last_run._workdir_path)
            return self._clean_workdir(last_run.workdir)
        all_workdirs = [path for path in tmt.utils.generate_runs(path, id_)]
        keep = self.opt('keep')
        if keep is not None:
            # Sort by modify time of the workdirs and keep the newest workdirs
            all_workdirs.sort(key=lambda workdir: os.path.getmtime(
                os.path.join(workdir, 'run.yaml')), reverse=True)
            all_workdirs = all_workdirs[keep:]

        successful = True
        for workdir in all_workdirs:
            if not self._clean_workdir(workdir):
                successful = False

        return successful


class Result(object):
    """
    Test result

    The following keys are expected in the 'data' dictionary::

        result ........... test execution result
        log .............. one or more log files
        note ............. additional result details
        duration.......... test execution time (hh:mm:ss)

    Required parameter 'name' should contain a unique test name.
    """

    _results = {
        'pass': 'green',
        'fail': 'red',
        'info': 'blue',
        'warn': 'yellow',
        'error': 'magenta',
        }

    def __init__(self, data, name):
        """
        Initialize test result data """

        # Save the test name and optional note
        if not name or not isinstance(name, str):
            raise tmt.utils.SpecificationError(f"Invalid test name '{name}'.")
        self.name = name
        self.note = data.get('note')
        self.duration = data.get('duration')

        # Check for valid results
        try:
            self.result = data['result']
        except KeyError:
            raise tmt.utils.SpecificationError("Missing test result.")
        if self.result not in self._results:
            raise tmt.utils.SpecificationError(
                f"Invalid result '{self.result}'.")

        # Convert log into list if necessary
        try:
            self.log = tmt.utils.listify(data['log'])
        except KeyError:
            self.log = []

    @staticmethod
    def total(results):
        """ Return dictionary with total stats for given results """
        stats = dict([(result, 0) for result in Result._results])
        for result in results:
            stats[result.result] += 1
        return stats

    @staticmethod
    def summary(results):
        """ Prepare a nice human summary of provided results """
        stats = Result.total(results)
        comments = []
        if stats.get('pass'):
            passed = ' ' + click.style('passed', fg='green')
            comments.append(fmf.utils.listed(stats['pass'], 'test') + passed)
        if stats.get('fail'):
            failed = ' ' + click.style('failed', fg='red')
            comments.append(fmf.utils.listed(stats['fail'], 'test') + failed)
        if stats.get('info'):
            count, comment = fmf.utils.listed(stats['info'], 'info').split()
            comments.append(count + ' ' + click.style(comment, fg='blue'))
        if stats.get('warn'):
            count, comment = fmf.utils.listed(stats['warn'], 'warn').split()
            comments.append(count + ' ' + click.style(comment, fg='yellow'))
        if stats.get('error'):
            count, comment = fmf.utils.listed(stats['error'], 'error').split()
            comments.append(count + ' ' + click.style(comment, fg='magenta'))
        return fmf.utils.listed(comments or ['no results found'])

    def show(self):
        """ Return a nicely colored result with test name (and note) """
        result = 'errr' if self.result == 'error' else self.result
        colored = style(result, fg=self._results[self.result])
        note = f" ({self.note})" if self.note else ''
        return f"{colored} {self.name}{note}"

    def export(self):
        """ Save result data for future wake-up """
        data = dict(result=self.result, log=self.log)
        if self.note:
            data['note'] = self.note
        if self.duration:
            data['duration'] = self.duration
        return data


class Link(object):
    """ Core attribute link parsing """

    # The list of all supported link relations
    _relations = [
        'verifies', 'verified-by',
        'implements', 'implemented-by',
        'documents', 'documented-by',
        'blocks', 'blocked-by',
        'duplicates', 'duplicated-by',
        'parent', 'child',
        'relates',
        ]

    # The list of valid fmf id keys
    _fmf_id_keys = ['url', 'ref', 'path', 'name']

    def __init__(self, data=None):
        """ Convert link data into canonical form """
        # Nothing to do if no data provided
        self.links = []
        if data is None:
            return
        if not isinstance(data, list):
            data = [data]

        # Ensure that each link is in the canonical form
        for link in data:

            # It should be a string
            if isinstance(link, str):
                self.links.append(dict(relates=link))
                continue

            # Or a dictionary
            if not isinstance(link, dict):
                raise tmt.utils.SpecificationError(
                    f"Invalid link target (should be 'str' or 'dict', "
                    f"got '{type(link).__name__}'.")

            # Verify the relation
            relations = []
            for key in link:
                # Skip fmf id keys and optional link note for now
                if key in self._fmf_id_keys + ['note']:
                    continue
                if key in self._relations:
                    relations.append(key)
                    continue
                raise tmt.utils.SpecificationError(
                    f"Invalid link relation '{key}' (should be "
                    f"{fmf.utils.listed(self._relations, join='or')}).")
            # More relations (error)
            if len(relations) > 1:
                raise tmt.utils.SpecificationError(
                    f"Multiple relations specified for the link "
                    f"({fmf.utils.listed(relations)}).")
            # No relation (fmf id)
            if len(relations) == 0:
                self.links.append(dict(relates=link))
                continue
            # The link should contain a relation and an optional note
            allowed_keys = set(relations + ['note'])
            extra_keys = set(link.keys()).difference(allowed_keys)
            if extra_keys:
                extra_keys = fmf.utils.listed(extra_keys, quote="'")
                raise tmt.utils.SpecificationError(
                    f"Unexpected link key {extra_keys}. Store the dictionary "
                    f"with the fmf id under the '{relations[0]}' key.")
            # Valid link
            self.links.append(link)

    def get(self, relation=None):
        """ Get links with given relation, all by default """
        return [
            link for link in self.links
            if relation in link or relation is None]

    def show(self):
        """ Format a list of links with their relations """
        for link in self.links:
            relation = [key for key in link.keys() if key != 'note'][0]
            echo(tmt.utils.format(
                relation.rstrip('-by'), f"{link[relation]}", key_color='cyan'))
