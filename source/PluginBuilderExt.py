"""MIT License

Copyright (c) 2024 Keith Lostracco

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import configparser
import os
import shutil
import subprocess
import threading
import queue
import json
import ast

import CMakeBlocks


# =================================================================================================
# Log prefixes used throughout this extension:
#
#   [Init]        — Initialization and constructor
#   [Create]      — Plugin project creation (scaffolding, templates, loader ops)
#   [Build]       — CMake configure step  (cmake -B build ...)
#   [Compile]     — Ninja build step      (ninja -C build)
#   [Build→Copy]  — Post-compile DLL copy from build output → __Plugins__/ (debounced)
#   [Install]     — Install plugin to ~/Documents/Derivative/Plugins/
#   [Source]      — Source file change callbacks
#   [CMake]       — CMakeLists.txt change callbacks
#   [Subprocess]  — cmd.exe subprocess lifecycle
#   [Cleanup]     — Teardown, unload, and child destruction
# =================================================================================================


class PluginBuilderExt:
	"""
	Creates, builds, compiles and installs C++ plugins for TouchDesigner.

	Lifecycle:
		1. create_plugin()   — scaffold a new plugin project from a template
		2. build_plugin()    — run CMake configure (generates Ninja build files)
		3. compile_plugin()  — run Ninja build (produces the .dll)
		4. OnPluginUpdate()  — auto-copy built DLL into __Plugins__/ (debounced)
		5. install_plugin()  — copy plugin to ~/Documents/Derivative/Plugins/

	The extension manages a persistent cmd.exe subprocess (with vcvarsall env)
	for running CMake and Ninja without re-initializing the MSVC environment
	on every build.
	"""

	# ========================================================================================== #
	#  INITIALIZATION                                                                            #
	# ========================================================================================== #

	def __init__(self, ownerComp):
		"""Initialize the PluginBuilder extension.

		Sets up component references, loads settings.ini config, validates
		required paths (PluginBuilderDir, NinjaDir, VCVarsall), starts the
		build subprocess, and schedules a deferred DAT refresh.
		"""
		# --- Core component references ---
		self.ownerComp = ownerComp
		self.builderComp = ownerComp.op('builder')
		self.parent = ownerComp.parent()

		# --- DAT references for file watching ---
		self.SettingsDat = self.builderComp.op('settings')       # settings.ini content
		self.folder_binDat = self.builderComp.op('folder_bin')   # watches build output dir
		self.sourceComp = ownerComp.op('source')
		self.folder_sourceDat = self.sourceComp.op('sync/folder_source')  # watches source dir
		self.CMakeListsDat = self.ownerComp.op('CMakeLists')

		# --- Config parsing ---
		self.user_home = os.environ.get('USERPROFILE', os.environ.get('HOME', ''))
		self.config = configparser.ConfigParser()
		self.config.read_string(self.SettingsDat.text)

		# --- Validate required tool paths ---
		self.PathsValid = False
		self.PathsValid = self.check_paths()

		# --- Dev mode (skips disabling create pars after plugin creation) ---
		self.dev_mode = False
		if self.config.has_section('DevMode'):
			self.dev_mode = True

		# --- Parameter callback dispatch maps ---
		self.on_par_value_change_map = {
			'Outputto': self.onOutputto,
			'Pluginname': self.onPluginname,
		}

		self.on_par_pulse_map = {
			'Createplugin': self.create_plugin,
			'Buildplugin': self.build_plugin,
			'Compileplugin': self.compile_plugin,
			'Closesubprocess': self.close_subprocess,
			'Installplugin': self.install_plugin,
			'Refreshcustompars': lambda: self.sync_custom_parameters(force_rebuild=True),
		}

		# --- Template definitions ---
		# Maps template menu names → plugin type, source file replacement name, and CMake assembler
		self.template_map = {
			'BasicCHOP':            {'type': 'CHOP', 'replace': 'BasicCHOP',           'assemble_cmake': self.assemble_cmake_text_basic},
			'CHOPWithPythonClass':  {'type': 'CHOP', 'replace': 'CHOPWithPythonClass', 'assemble_cmake': self.assemble_cmake_text_python},
			'CPUMemoryTOP':         {'type': 'TOP',  'replace': 'CPUMemoryTOP',        'assemble_cmake': self.assemble_cmake_text_basic},
			'CudaTOP':              {'type': 'TOP',  'replace': 'CudaTOP',             'assemble_cmake': self.assemble_cmake_text_cuda},
			'BasicDAT':             {'type': 'DAT',  'replace': 'BasicDAT',            'assemble_cmake': self.assemble_cmake_text_basic},
			'SimpleShapesSOP':      {'type': 'SOP',  'replace': 'SimpleShapesSOP',     'assemble_cmake': self.assemble_cmake_text_basic},
		}

		# --- Loader op types by plugin type (CHOP/TOP/DAT/SOP) ---
		self.loader_op_map = {
			'CHOP': {'loader': cplusplusCHOP, 'in': inCHOP, 'out': outCHOP},
			'TOP':  {'loader': cplusplusTOP,  'in': inTOP,  'out': outTOP},
			'DAT':  {'loader': cplusplusDAT,  'in': inDAT,  'out': outDAT},
			'SOP':  {'loader': cplusplusSOP,  'in': inSOP,  'out': outSOP},
		}

		# --- Directory conventions ---
		self.plugin_projects_dir = 'PluginProjects'  # where plugin source projects live
		self.plugins_dir = '__Plugins__'              # where built DLLs go (local)

		# --- Subprocess state ---
		self.process = None
		self.queue = None
		if self.start_subprocess():
			self.build_plugin()

		# --- Plugin loader reference (may be None if no plugin is loaded) ---
		self.loader_op = self.ownerComp.op('plugin_loader')

		# --- Deferred DAT refresh (wait for TD to finish loading) ---
		run("args[0].RefreshDats()", self.ownerComp, delayFrames=120)

		# --- Retry counter for locked-file copy attempts ---
		self.open_attempts = 0

		# --- Ensure 'Custom' parameter tab exists and sync custom parameters ---
		self.sync_custom_parameters()

		self._log('Init', f"PluginBuilderExt initialized (dev_mode={self.dev_mode}, "
				  f"paths_valid={self.PathsValid}, plugin='{self.Pluginname}')")

	def __del__(self):
		"""Destructor — ensures the build subprocess is terminated."""
		self.close_subprocess()

	# ========================================================================================== #
	#  LOGGING                                                                                   #
	# ========================================================================================== #

	@staticmethod
	def _log(tag, message):
		"""Print a tagged log message.

		All log output from this extension uses this method for consistency.
		Tags are short identifiers like 'Init', 'Build', 'Install', etc.

		Args:
			tag: Short category label (e.g. 'Build→Copy', 'Install').
			message: The log message content.
		"""
		print(f"[{tag}] {message}")

	# ========================================================================================== #
	#  PROPERTIES                                                                                #
	# ========================================================================================== #

	@property
	def start_subprocess_base_cmd(self):
		"""Base command to start the MSVC-initialized cmd.exe subprocess.

		Runs vcvarsall.bat for x64, then appends Ninja's directory to PATH
		so both cmake and ninja are available without full paths.
		"""
		cmd = ['cmd.exe', '/K', self.vcvarsall, 'x64']

		# Add ninja to PATH so it's available alongside cmake
		cmd.append('&&')
		cmd.append(f'set PATH=%PATH%;{self.ninja_dir}')
		return cmd

	@property
	def cmake_build_cmd(self):
		"""CMake configure command string.

		Generates Ninja build files with the specified build config
		(Debug/Release/RelWithDebInfo). Sets PLUGINBUILDER_BUILD env var
		so CMakeLists.txt can detect PluginBuilder-driven builds.
		"""
		config = self.ownerComp.par.Buildconfig.eval()
		cmd = (f'set PLUGINBUILDER_BUILD="" && cmake -B build -G Ninja '
			   f'-DPLUGIN_BUILDER_DIR={self.PluginBuilderDir} '
			   f'-DPLUGIN_DIR={self.plugin_dir} '
			   f'-DCMAKE_BUILD_TYPE={config}')
		return cmd

	@property
	def cmake_clean_cmd(self):
		"""Ninja clean command string."""
		return "ninja -C build clean"

	@property
	def cmake_build_plugin_cmd(self):
		"""Ninja build command string (compiles the plugin DLL)."""
		return "ninja -C build"

	@property
	def working_dir(self):
		"""Relative path to the plugin's project directory (e.g. PluginProjects/FFT)."""
		return f"{self.plugin_projects_dir}/{self.Pluginname}"

	@property
	def abs_working_dir(self):
		"""Absolute path to the plugin's project directory."""
		return f"{project.folder}/{self.working_dir}"

	@property
	def plugin_dir(self):
		"""Relative path to the plugin's output directory (e.g. __Plugins__/FFT)."""
		return f"{self.plugins_dir}/{self.Pluginname}"

	@property
	def Pluginname(self):
		"""Current plugin name from the component's Pluginname parameter."""
		return self.ownerComp.par.Pluginname.eval()

	@property
	def CMakeListsPath(self):
		"""Relative path to the plugin's CMakeLists.txt."""
		return f"{self.working_dir}/CMakeLists.txt"

	@property
	def CMakeListsExists(self):
		"""True if the plugin's CMakeLists.txt exists on disk."""
		return os.path.exists(self.CMakeListsPath)

	@property
	def build_config(self):
		"""Current build configuration (Debug/Release/RelWithDebInfo)."""
		return self.ownerComp.par.Buildconfig.eval()

	@property
	def PluginBuilderDir(self):
		"""Absolute path to the PluginBuilder installation directory."""
		return self.get_path('Paths', 'PluginBuilderDir')

	@property
	def SourceDir(self):
		"""Relative path to the plugin's source directory."""
		return f"{self.working_dir}/source"

	@property
	def ninja_dir(self):
		"""Absolute path to the directory containing ninja.exe."""
		return self.get_path('Paths', 'NinjaDir')

	@property
	def template_dir(self):
		"""Absolute path to the PluginBuilder templates directory."""
		return f"{self.PluginBuilderDir}/templates"

	@property
	def vcvarsall(self):
		"""Absolute path to vcvarsall.bat (MSVC environment setup)."""
		return self.get_path('Paths', 'VCVarsall')

	@property
	def CurrentBinDir(self):
		"""Relative path to the build output bin directory (config-specific)."""
		return f"PluginProjects/{self.Pluginname}/build/bin/{self.build_config}"

	@property
	def TDProjectName(self):
		"""TouchDesigner project name (without version/extension suffixes)."""
		vals = project.name.split('.')
		if len(vals) > 2:
			return ''.join(vals[:-2])
		return vals[0]

	@property
	def TDPath(self):
		"""Absolute path to TouchDesigner.exe."""
		return f"{app.binFolder}/TouchDesigner.exe"

	@property
	def PluginPath(self):
		"""Relative path to the plugin DLL in __Plugins__/."""
		return f"{self.plugin_dir}/{self.Pluginname}.dll"

	@property
	def build_path(self):
		"""Relative path to the built DLL in the build output directory."""
		return f"{self.CurrentBinDir}/{self.Pluginname}.dll"

	@property
	def CompileOnUpdate(self):
		"""Whether to auto-compile when source files change."""
		return self.ownerComp.par.Compileonupdate.eval()

	# ========================================================================================== #
	#  INTERNAL METHODS                                                                          #
	# ========================================================================================== #

	def get_path(self, section, key):
		"""Resolve a path from settings.ini, expanding ${USER_PATH} placeholders.

		Args:
			section: INI section name (e.g. 'Paths').
			key: INI key name (e.g. 'NinjaDir').

		Returns:
			Expanded absolute path string.
		"""
		return self.config.get(section, key).replace('${USER_PATH}', self.user_home)

	# ---------------- Plugin Creation --------------------------------------------------------- #

	def create_plugin(self):
		"""Create a new plugin project from a template.

		Steps:
			1. Validate plugin name is not empty and project doesn't already exist
			2. Generate CMakeLists.txt from template blocks
			3. Copy CMakePresets.json and generate launch.vs.json for VS debugging
			4. Copy and rename template source files (replacing placeholder names)
			5. Create output directories (__Plugins__/<name>/)
			6. Start subprocess, run CMake configure + Ninja build
			7. Create the plugin loader op chain (in → cplusplus → out)
			8. Lock creation parameters (unless dev_mode)

		Raises:
			ValueError: If the plugin name is empty.
			FileExistsError: If the project directory already exists.
		"""
		name = self.Pluginname
		if name == '':
			raise ValueError("Plugin name is empty.")

		self._log('Create', f"Creating new plugin project '{name}'...")

		os.makedirs(self.plugin_projects_dir, exist_ok=True)

		if os.path.exists(self.working_dir):
			raise FileExistsError(
				f"Directory {self.working_dir} already exists. "
				"Rename plugin, change working directory or delete existing directory."
			)

		template_name = self.ownerComp.par.Plugintemplate.eval()
		template_info = self.template_map.get(template_name)
		self._log('Create', f"Using template '{template_name}' (type={template_info.get('type')})")

		os.makedirs(self.working_dir)

		try:
			# --- Generate CMakeLists.txt from template blocks ---
			cmake_text = template_info.get('assemble_cmake')()
			cmake_text = cmake_text.replace('__PLUGIN_NAME__', self.Pluginname)
			cmake_text = cmake_text.replace('__PLUGIN_TYPE__', f"'{template_info.get('type')}'")
			cmake_text = cmake_text.replace('__PLUGIN_BUILDER_DIR__', f'"{self.PluginBuilderDir}"')

			with open(f"{self.working_dir}/CMakeLists.txt", 'w') as f:
				f.write(cmake_text)
			self._log('Create', f"Generated CMakeLists.txt")

			# --- Copy CMakePresets.json ---
			shutil.copyfile(
				f"{self.PluginBuilderDir}/source/CMakePresets.json",
				f"{self.working_dir}/CMakePresets.json"
			)
			self._log('Create', f"Copied CMakePresets.json")

			# --- Generate launch.vs.json (Visual Studio debug config) ---
			with open(f"{self.PluginBuilderDir}/source/launch.vs.json", 'r') as f:
				launch_vs_json = json.load(f)

			project_name = self.TDProjectName
			td_path = self.TDPath
			for config in launch_vs_json['configurations']:
				config['name'] = config['name'].replace('__TD_PROJECT_NAME__', project_name)
				config['args'][0] = config['args'][0].replace('__TD_PROJECT_NAME__', project_name)
				config['projectTarget'] = config['projectTarget'].replace('__PLUGIN_NAME__', self.Pluginname)
				config['exe'] = config['exe'].replace('__TD_PATH__', td_path)

			with open(f"{self.working_dir}/launch.vs.json", 'w') as f:
				json.dump(launch_vs_json, f, indent=4)
			self._log('Create', f"Generated launch.vs.json (TD project: '{project_name}')")

			# --- Copy and rename template source files ---
			os.makedirs(f"{self.working_dir}/source")
			template_replace_name = template_info.get('replace')
			template_source_dir = f"{self.template_dir}/{template_name}/source"
			source_files = os.listdir(template_source_dir)
			self._log('Create', f"Copying {len(source_files)} template source file(s) from {template_name}/source/")

			for file_name in source_files:
				with open(f"{template_source_dir}/{file_name}", 'r') as f:
					text = f.read()

				# Replace template class/file names with plugin name
				text = text.replace(template_replace_name, self.Pluginname)

				# For the main .cpp file, also replace operator metadata placeholders
				if file_name == f"{template_replace_name}.cpp":
					text = text.replace('#__OP_TYPE__#', self.Pluginname.capitalize())
					text = text.replace('#__OP_LABEL__#', self.Pluginname)
					text = text.replace('#__OP_ICON__#', self.Pluginname[:3].upper())
					text = text.replace('#__OP_AUTHOR__#', self.config.get('PluginInfo', 'Author'))
					text = text.replace('#__OP_EMAIL__#', self.config.get('PluginInfo', 'Email'))

				file_name = file_name.replace(template_replace_name, self.Pluginname)

				with open(f"{self.working_dir}/source/{file_name}", 'w') as f:
					f.write(text)

			# --- Create output directories ---
			os.makedirs(self.plugins_dir, exist_ok=True)
			os.makedirs(f"{self.plugin_dir}", exist_ok=True)
			self._log('Create', f"Created output directories")

			# --- Run initial build ---
			if self.start_subprocess():
				self.build_plugin()
				self.compile_plugin()
				self._log('Create', f"Initial build + compile triggered")

		except Exception as e:
			self._log('Create', f"ERROR: {e} — cleaning up project directory")
			shutil.rmtree(self.working_dir)
			raise e

		# --- Create loader op chain ---
		self.create_plugin_loader(template_info.get('type'))
		run("args[0].PostCreatePlugin()", self.ownerComp, delayFrames=300)
		if not self.dev_mode:
			self.disable_create_pars()

		self._log('Create', f"Plugin '{name}' created successfully")

	def destroy_children(self):
		"""Destroy all child ops except the core builder/source/CMakeLists components."""
		children = self.ownerComp.findChildren(depth=1)
		preserved = ['builder', 'source', 'CMakeLists']
		for child in children:
			if child.name not in preserved:
				child.destroy()

	def create_plugin_loader(self, plugin_type):
		"""Create the plugin loader op chain (input → cplusplus loader → output).

		Args:
			plugin_type: One of 'CHOP', 'TOP', 'DAT', 'SOP'.
		"""
		self._log('Create', f"Creating plugin loader chain (type={plugin_type})")
		self.destroy_children()

		loader_op_info = self.loader_op_map.get(plugin_type)

		# Create ops: optional input → loader → output
		create_input_op = self.ownerComp.par.Createinputop.eval()
		if create_input_op:
			in_op = self.ownerComp.create(loader_op_info.get('in'), 'in1')
		self.loader_op = self.ownerComp.create(loader_op_info.get('loader'), 'plugin_loader')
		out_op = self.ownerComp.create(loader_op_info.get('out'), 'out1')

		# Position ops in the network
		if create_input_op:
			in_op.nodeX = -200
		self.loader_op.nodeX = 0
		out_op.nodeX = 200

		# Wire connections
		if create_input_op:
			in_op.outputConnectors[0].connect(self.loader_op.inputConnectors[0])
		self.loader_op.outputConnectors[0].connect(out_op.inputConnectors[0])

		# Configure loader — start unloaded, set plugin path
		self.loader_op.par.unloadplugin = True
		self.loader_op.par.plugin = f"{self.plugin_dir}/{self.Pluginname}.dll"

		# Schedule custom parameter sync after loader initialization
		run("args[0].ext.PluginBuilderExt.sync_custom_parameters()", self.ownerComp, delayFrames=15)

	# ---------------- CMake Assembly ---------------------------------------------------------- #

	def assemble_cmake_text_basic(self):
		"""Assemble CMakeLists.txt for a basic plugin (CHOP/TOP/DAT/SOP)."""
		return CMakeBlocks.start_block + CMakeBlocks.project_block + CMakeBlocks.core_block

	def assemble_cmake_text_cuda(self):
		"""Assemble CMakeLists.txt for a CUDA-enabled TOP plugin."""
		return CMakeBlocks.start_block + CMakeBlocks.cuda_project_block + CMakeBlocks.core_block + CMakeBlocks.cuda_block

	def assemble_cmake_text_python(self):
		"""Assemble CMakeLists.txt for a CHOP plugin with embedded Python."""
		return CMakeBlocks.start_block + CMakeBlocks.project_block + CMakeBlocks.core_block + CMakeBlocks.python_block

	# ---------------- Build & Compile --------------------------------------------------------- #

	def build_plugin(self):
		"""Run CMake configure step (generates Ninja build files).

		Sends the cmake command to the subprocess. Requires the working
		directory and CMakeLists.txt to exist.

		Raises:
			FileNotFoundError: If the working directory doesn't exist.
		"""
		if not os.path.exists(self.abs_working_dir):
			raise FileNotFoundError(f"Directory {self.abs_working_dir} does not exist.")

		# Clean stale build cache if generator mismatched or non-Ninja cache detected
		build_dir = os.path.join(self.abs_working_dir, 'build')
		cache_file = os.path.join(build_dir, 'CMakeCache.txt')
		if os.path.exists(cache_file):
			try:
				with open(cache_file, 'r', encoding='utf-8', errors='ignore') as f:
					content = f.read()
					if 'CMAKE_GENERATOR:INTERNAL=' in content and 'Ninja' not in content:
						self._log('Build', "Detected non-Ninja CMake generator cache. Cleaning build directory...")
						shutil.rmtree(build_dir, ignore_errors=True)
			except Exception as e:
				self._log('Build', f"Warning inspecting CMakeCache.txt: {e}")

		if self.CMakeListsExists:
			self._log('Build', "=========================================================")
			self._log('Build', f"STARTING CMAKE CONFIGURE FOR PLUGIN: '{self.Pluginname}'")
			self._log('Build', f"  - Target Plugin: '{self.Pluginname}'")
			self._log('Build', f"  - Build Config:  '{self.build_config}'")
			self._log('Build', f"  - Working Dir:   {self.abs_working_dir}")
			self._log('Build', f"  - CMakeLists:    {self.CMakeListsPath} (EXISTS)")
			self._log('Build', f"  - Executing:     {self.cmake_build_cmd}")
			self.SendCommand(self.cmake_build_cmd)
		else:
			self._log('Build', f"SKIPPED — no CMakeLists.txt found at {self.CMakeListsPath}")

	def compile_plugin(self):
		"""Run Ninja build step (compiles the plugin DLL).

		Sends the ninja command to the subprocess. Requires CMakeLists.txt
		to exist (i.e., cmake configure must have been run first).
		"""
		if self.CMakeListsExists:
			self._log('Compile', "=========================================================")
			self._log('Compile', f"STARTING NINJA BUILD FOR PLUGIN: '{self.Pluginname}'")
			self._log('Compile', f"  - Target Plugin: '{self.Pluginname}'")
			self._log('Compile', f"  - Build Output:  {self.build_path}")
			self._log('Compile', f"  - Executing:     {self.cmake_build_plugin_cmd}")
			self.SendCommand(self.cmake_build_plugin_cmd)
		else:
			self._log('Compile', "SKIPPED — no CMakeLists.txt found")

	def BuildAndCompile(self):
		"""Run both CMake configure and Ninja build in sequence."""
		self.build_plugin()
		self.compile_plugin()

	def RefreshDats(self):
		"""Force-cook all file-watching DATs to pick up filesystem changes.

		Called on a delay after initialization to ensure DATs reflect
		the current state of the project files.
		"""
		self._log('Init', "Refreshing file-watching DATs...")
		self.folder_binDat.cook(force=True)
		self.folder_sourceDat.cook(force=True)
		self.CMakeListsDat.cook(force=True)
		self.sync_custom_parameters()

	# ---------------- Cleanup ----------------------------------------------------------------- #

	def clear_plugin_builder(self):
		"""Unload the current plugin, destroy loader ops, and reset the plugin name.

		Called when the Pluginname parameter is cleared.
		"""
		self._log('Cleanup', f"Clearing plugin builder (current: '{self.Pluginname}')")

		self.loader_op = self.ownerComp.op('plugin_loader')
		if self.loader_op is not None:
			self.loader_op.par.unloadplugin = True
			self.loader_op.cook(force=True)
			self._log('Cleanup', "Plugin unloaded")

		self.destroy_children()

		# Reset parameters on 'Custom' page without deleting the tab
		page = self._get_custom_page(self.ownerComp, 'Custom')
		if page:
			for p in list(page.pars):
				try:
					p.destroy()
				except Exception:
					pass

		if self.Pluginname != '':
			self.ownerComp.par.Pluginname = ''

		self._log('Cleanup', "Plugin builder cleared")

	# ---------------- Install ----------------------------------------------------------------- #

	def install_plugin(self):
		"""Install the plugin to TouchDesigner's user plugins directory."""
		self._log('Install', "=========================================================")
		self._log('Install', f"STARTING PLUGIN INSTALLATION FOR: '{self.Pluginname}'")
		self._log('Install', f"  Step 1/4: Inspecting source directory ({self.plugin_dir})...")

		if not os.path.exists(self.plugin_dir):
			self._log('Install', f"  ERROR: Source folder {self.plugin_dir} does not exist. Aborting.")
			return

		src_files = []
		for root, dirs, files in os.walk(self.plugin_dir):
			for f in files:
				src_files.append(os.path.join(root, f))
		self._log('Install', f"  Source contains {len(src_files)} file(s):")
		for f in src_files:
			size = os.path.getsize(f)
			self._log('Install', f"    - {f} ({size} bytes)")

		install_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Derivative', 'Plugins')
		self._log('Install', f"  Step 2/4: Resolving target installation folder...")
		self._log('Install', f"    - Target Dir: {install_dir}")

		if not os.path.exists(install_dir):
			self._log('Install', f"  ERROR: Target folder {install_dir} does not exist. Aborting.")
			return

		loader_op = self.ownerComp.op('plugin_loader')
		self._log('Install', f"  Step 3/4: Unloading plugin in loader op to release DLL file locks...")
		if loader_op is not None:
			loader_op.par.unloadplugin = True
			loader_op.cook(force=True)
			self._log('Install', f"    - Unloaded plugin from {loader_op.path}")
		else:
			self._log('Install', "    - WARNING: No plugin_loader op found, skipping unload.")

		dest = os.path.join(install_dir, self.Pluginname)
		self._log('Install', f"  Step 4/4: Copying binaries to user plugins directory...")
		self._log('Install', f"    - Destination Path: {dest}")

		def _force_copy(src, dst):
			basename = os.path.basename(dst)
			if os.path.exists(dst):
				try:
					os.remove(dst)
					self._log('Install', f"    - Overwrote existing {basename}")
				except PermissionError:
					self._log('Install', f"    - {basename} is locked, renaming to .old...")
					old = dst + '.old'
					if os.path.exists(old):
						try:
							os.remove(old)
						except PermissionError:
							pass
					try:
						os.rename(dst, old)
						self._log('Install', f"    - Renamed {basename} -> {basename}.old")
					except OSError as e:
						self._log('Install', f"    - ERROR: Could not rename locked file {dst}: {e}")
			else:
				self._log('Install', f"    - Copying new file: {basename}")

			shutil.copy2(src, dst)
			size = os.path.getsize(dst)
			self._log('Install', f"    - Successfully copied {basename} ({size} bytes)")

		shutil.copytree(self.plugin_dir, dest, dirs_exist_ok=True, copy_function=_force_copy)

		if loader_op is not None:
			self._log('Install', "  Re-loading plugin into TouchDesigner...")
			loader_op.par.unloadplugin = False
			if hasattr(loader_op.par, 'reinitpulse'):
				loader_op.par.reinitpulse.pulse()
			loader_op.cook(force=True)
			run("args[0].ext.PluginBuilderExt.sync_custom_parameters(verbose=True)", self.ownerComp, delayFrames=15)

		self._log('Install', f"INSTALLATION COMPLETE: Plugin '{self.Pluginname}' ready in Derivative/Plugins.")

	# ---------------- Path Validation --------------------------------------------------------- #

	def check_paths(self):
		"""Validate that all required tool paths from settings.ini exist.

		Checks:
			- PluginBuilderDir (this repo's install location)
			- NinjaDir (directory containing ninja.exe)
			- VCVarsall (path to vcvarsall.bat for MSVC)

		Returns:
			True if all paths are valid.

		Raises:
			FileNotFoundError: If any required path doesn't exist.
		"""
		value = self.get_path('Paths', 'PluginBuilderDir')
		if not os.path.exists(value):
			raise FileNotFoundError(f"settings.ini [paths] PluginBuilderDir: {value} does not exist.")

		value = self.get_path('Paths', 'NinjaDir')
		if not os.path.exists(value):
			raise FileNotFoundError(f"settings.ini [paths] NinjaDir: {value} does not exist.")

		value = self.get_path('Paths', 'VCVarsall')
		if not os.path.exists(value):
			raise FileNotFoundError(f"settings.ini [paths] VCVarsall: {value} does not exist.")

		return True

	# ---------------- UI Helpers -------------------------------------------------------------- #

	def disable_create_pars(self):
		"""Lock plugin creation parameters (called after a plugin is created).

		Prevents accidental changes to plugin name/template while a project
		is active. Skipped in dev_mode.
		"""
		self.ownerComp.par.Createplugin.enable = False
		self.ownerComp.par.Pluginname.readOnly = True
		self.ownerComp.par.Plugintemplate.readOnly = True
		self.ownerComp.par.Createinputop.enable = False

	def PostCreatePlugin(self):
		"""Deferred post-creation hook — refreshes the CMakeLists DAT.

		Called ~300 frames after plugin creation to ensure the filesystem
		has settled before the DAT tries to read the file.
		"""
		self.CMakeListsDat.cook(force=True)

	def file_locked(self, filepath):
		"""Check if a file is locked (in use by another process).

		Attempts to open the file in binary mode with no buffering.
		If a PermissionError is raised, the file is locked.

		Args:
			filepath: Path to the file to check.

		Returns:
			True if the file is locked, False otherwise (including if
			the file doesn't exist).
		"""
		try:
			with open(filepath, 'rb', buffering=0):
				pass
		except FileNotFoundError:
			return False  # File doesn't exist — not "locked"
		except (PermissionError, OSError):
			return True   # File is locked by another process
		return False

	# ========================================================================================== #
	#  EXTERNAL METHODS (promoted to component, callable from other ops)                         #
	# ========================================================================================== #

	def EnableCreatePars(self):
		"""Unlock plugin creation parameters (re-enable the Create button)."""
		self.ownerComp.par.Createplugin.enable = True
		self.ownerComp.par.Pluginname.readOnly = False
		self.ownerComp.par.Plugintemplate.readOnly = False
		self.ownerComp.par.Createinputop.enable = True

	# ========================================================================================== #
	#  CUSTOM PARAMETER SYNC                                                                     #
	# ========================================================================================== #

	@staticmethod
	def _get_custom_page(op, page_name):
		"""Retrieve a custom page by name from an operator, or None if not found."""
		if op is None or not getattr(op, 'valid', True):
			return None
		for page in getattr(op, 'customPages', []):
			if page.name == page_name:
				return page
		return None

	@staticmethod
	def _copy_par_attributes(src_p, dst_p):
		"""Safely copy attributes from source parameter to destination parameter."""
		attrs = ['default', 'min', 'max', 'normMin', 'normMax', 'clampMin', 'clampMax', 'val', 'enable', 'readOnly']
		for attr in attrs:
			if hasattr(src_p, attr):
				try:
					setattr(dst_p, attr, getattr(src_p, attr))
				except Exception:
					pass
		if hasattr(src_p, 'menuNames') and hasattr(dst_p, 'menuNames'):
			try:
				if hasattr(src_p, 'menuLabels') and hasattr(dst_p, 'menuLabels'):
					dst_p.menuLabels = src_p.menuLabels
				dst_p.menuNames = src_p.menuNames
			except Exception:
				try:
					dst_p.menuNames = src_p.menuNames
					if hasattr(src_p, 'menuLabels') and hasattr(dst_p, 'menuLabels'):
						dst_p.menuLabels = src_p.menuLabels
				except Exception:
					pass

	def _sync_single_parameter(self, loader_par, page, target_comp):
		"""Dynamically mirror a single parameter from loader_par onto target_comp's page.

		Adapts to newly created, renamed, or modified parameters by inspecting
		parameter style, size, bounds, menus, bindings, and enablement across all
		tuple components.
		"""
		par_name = loader_par.name
		label = getattr(loader_par, 'label', par_name)
		style = getattr(loader_par, 'style', '')
		is_pulse = getattr(loader_par, 'isPulse', False) or style == 'Pulse'

		# Determine tuple size if available
		size = 1
		if hasattr(loader_par, 'tuple') and loader_par.tuple:
			try:
				size = len(loader_par.tuple)
			except Exception:
				size = 1

		existing = getattr(target_comp.par, par_name, None)
		existing_page_name = getattr(getattr(existing, 'page', None), 'name', '') if existing is not None else ''

		# Detect style/type mismatch between existing parameter and C++ loader parameter
		if existing is not None:
			src_style = getattr(loader_par, 'style', '')
			dst_style = getattr(existing, 'style', '')

			src_is_menu = getattr(loader_par, 'isMenu', False) or src_style in ('Menu', 'StrMenu', 'IntMenu')
			dst_is_menu = getattr(existing, 'isMenu', False) or dst_style in ('Menu', 'StrMenu', 'IntMenu')

			src_is_float = getattr(loader_par, 'isFloat', False) or src_style == 'Float'
			dst_is_float = getattr(existing, 'isFloat', False) or dst_style == 'Float'

			src_is_int = getattr(loader_par, 'isInt', False) or src_style == 'Int'
			dst_is_int = getattr(existing, 'isInt', False) or dst_style == 'Int'

			src_is_toggle = getattr(loader_par, 'isToggle', False) or src_style == 'Toggle'
			dst_is_toggle = getattr(existing, 'isToggle', False) or dst_style == 'Toggle'

			if (src_is_menu != dst_is_menu or
				src_is_float != dst_is_float or
				src_is_int != dst_is_int or
				is_pulse != (getattr(existing, 'isPulse', False) or dst_style == 'Pulse') or
				src_is_toggle != dst_is_toggle):
				try:
					existing.destroy()
				except Exception:
					pass
				existing = None

		# Create parameter dynamically if missing, mismatched type, or not on target page
		if existing is None or existing_page_name != page.name:
			try:
				if getattr(loader_par, 'isFloat', False) or style == 'Float':
					res = page.appendFloat(par_name, label=label, size=size)
				elif getattr(loader_par, 'isInt', False) or style == 'Int':
					res = page.appendInt(par_name, label=label, size=size)
				elif getattr(loader_par, 'isToggle', False) or style == 'Toggle':
					res = page.appendToggle(par_name, label=label, size=size)
				elif getattr(loader_par, 'isMenu', False) or style in ('Menu', 'StrMenu', 'IntMenu'):
					try:
						res = page.appendMenu(par_name, label=label)
					except Exception:
						res = page.appendStr(par_name, label=label)
				elif is_pulse:
					res = page.appendPulse(par_name, label=label)
				elif getattr(loader_par, 'isStr', False) or getattr(loader_par, 'isString', False) or style in ('Str', 'String'):
					res = page.appendStr(par_name, label=label)
				elif getattr(loader_par, 'isHeader', False) or style == 'Header':
					res = page.appendHeader(par_name, label=label)
				elif style == 'XYZ':
					res = page.appendXYZ(par_name, label=label)
				elif style == 'UV':
					res = page.appendUV(par_name, label=label)
				elif style == 'RGB':
					res = page.appendRGB(par_name, label=label)
				elif style == 'RGBA':
					res = page.appendRGBA(par_name, label=label)
				else:
					try:
						res = page.appendStr(par_name, label=label)
					except Exception:
						res = page.appendPar(par_name, label=label)

				if isinstance(res, (list, tuple)):
					existing = res[0]
				else:
					existing = res
			except Exception as e:
				self._log('ParamSync', f"  ERROR creating dynamic parameter '{par_name}': {e}")
				return None

		# Collect destination tuple elements and source tuple elements
		if hasattr(existing, 'tuple') and existing.tuple:
			dst_pars = list(existing.tuple)
		else:
			dst_pars = [existing]

		if hasattr(loader_par, 'tuple') and loader_par.tuple:
			src_pars = list(loader_par.tuple)
		else:
			src_pars = [loader_par]

		# Process attribute copying and binding across all vector components
		for src_p, dst_p in zip(src_pars, dst_pars):
			if dst_p is not None:
				self._copy_par_attributes(src_p, dst_p)
				default_val = repr(getattr(src_p, 'default', 0))
				safe_bind = f"me.op('{self.loader_op.name}').par.{dst_p.name} if me.op('{self.loader_op.name}') is not None else {default_val}"

				try:
					dst_p.enable = getattr(src_p, 'enable', True)
					dst_p.readOnly = getattr(src_p, 'readOnly', False)
					if is_pulse:
						if hasattr(dst_p, 'bindExpr'):
							dst_p.bindExpr = ''
					else:
						if hasattr(dst_p, 'bindExpr'):
							dst_p.bindExpr = safe_bind
						try:
							if hasattr(td, 'ParMode'):
								dst_p.mode = td.ParMode.BIND
						except Exception:
							pass
						if hasattr(src_p, 'val'):
							dst_p.val = src_p.val
						elif hasattr(src_p, 'eval'):
							dst_p.val = src_p.eval()
				except Exception:
					pass

		return existing

	def sync_custom_parameters(self, force_rebuild=False, verbose=False):
		"""Sync custom parameters from plugin_loader onto ownerComp's 'Custom' tab.

		Mirrors all custom parameters from the plugin_loader operator (e.g. CPlusPlus CHOP)
		onto a 'Custom' parameter page on ownerComp. Establishes parameter bindings and
		synchronizes values.
		"""
		# Always ensure the 'Custom' page tab exists on ownerComp
		page = self._get_custom_page(self.ownerComp, 'Custom')
		if not page:
			try:
				page = self.ownerComp.appendCustomPage('Custom')
			except Exception as e:
				self._log('ParamSync', f"Could not create 'Custom' page: {e}")
				return

		self.loader_op = self.ownerComp.op('plugin_loader')
		if self.loader_op is None or not getattr(self.loader_op, 'valid', True):
			return

		# Force cook on loader_op so TouchDesigner updates parameter definitions
		try:
			self.loader_op.cook(force=True)
		except Exception:
			pass

		# Built-in parameters of CPlusPlus OPs to exclude
		built_in_pars = {
			'unloadplugin', 'plugin', 'reinit', 'reinitpulse',
			'timeslice', 'scope', 'srselect', 'exportmethod',
			'autoexportroot', 'exporttable', 'commonrenamefrom', 'commonrenameto',
			'outputresolution', 'resolutionw', 'resolutionh', 'aspect', 'aspectw', 'aspecth',
			'fill', 'filter', 'coord', 'format', 'pixelformat', 'colorformat',
			'pageindex', 'renamefrom', 'renameto'
		}

		# Collect custom C++ plugin parameters from plugin_loader (must start with uppercase letter)
		loader_custom_pars = []
		seen_names = set()
		try:
			all_pars = self.loader_op.pars()
		except Exception:
			all_pars = []

		for p in all_pars:
			par_name = p.name.lower()
			is_custom_par = getattr(p, 'isCustom', False) or (p.name and p.name[0].isupper())
			if is_custom_par and par_name not in built_in_pars:
				if p.name not in seen_names:
					seen_names.add(p.name)
					loader_custom_pars.append(p)

		current_page_par_names = [p_custom.name for p_custom in getattr(page, 'pars', [])]
		expected_par_names = [p.name for p in loader_custom_pars]

		# If force_rebuild is requested OR if parameter order/names mismatch, clean page first to preserve exact C++ declaration order
		if (force_rebuild or current_page_par_names != expected_par_names) and page and hasattr(page, 'pars'):
			for p_custom in list(page.pars):
				try:
					p_custom.destroy()
				except Exception:
					pass

		loader_par_names = {p.name for p in loader_custom_pars}

		# Remove any parameters on 'Custom' page that no longer exist on plugin_loader
		if page and hasattr(page, 'pars'):
			for p_custom in list(page.pars):
				if p_custom.name not in loader_par_names:
					try:
						p_custom.destroy()
					except Exception:
						pass

		# Ensure all pulse parameters on 'Custom' page have empty bindExpr
		if page and hasattr(page, 'pars'):
			for p_custom in page.pars:
				if getattr(p_custom, 'isPulse', False) or getattr(p_custom, 'style', '') == 'Pulse':
					try:
						if hasattr(p_custom, 'bindExpr'):
							p_custom.bindExpr = ''
					except Exception:
						pass

		for p in loader_custom_pars:
			self._sync_single_parameter(p, page, self.ownerComp)

		final_pars = [p_custom.name for p_custom in getattr(page, 'pars', [])]
		last_synced = getattr(self, '_last_synced_pars', None)
		if verbose or final_pars != last_synced:
			self._log('ParamSync', "=========================================================")
			self._log('ParamSync', f"DYNAMIC PARAMETER SYNC COMPLETE ON 'Custom' TAB:")
			self._log('ParamSync', f"  - Active Custom Parameters: {len(loader_custom_pars)}")
			for idx, p in enumerate(loader_custom_pars, start=1):
				style = getattr(p, 'style', 'Par')
				val = getattr(p, 'val', getattr(p, 'eval', lambda: '')())
				is_pulse = getattr(p, 'isPulse', False) or style == 'Pulse'
				mode_str = "OnParPulse" if is_pulse else f"BIND -> {self.loader_op.name}.par.{p.name}"
				self._log('ParamSync', f"  {idx}. {p.name:<14} [{style:<6}] val={val!r:<10} ({mode_str})")
			self._log('ParamSync', "=========================================================")
			self._last_synced_pars = final_pars

	# ========================================================================================== #
	#  PARAMETER CALLBACKS                                                                       #

	def OnParValueChange(self, par, prev):
		"""Dispatch parameter value changes to registered handlers."""
		if par.name in self.on_par_value_change_map:
			self.on_par_value_change_map[par.name](par.eval(), prev)
		elif self.loader_op is not None and getattr(self.loader_op, 'valid', True):
			loader_par = getattr(self.loader_op.par, par.name, None)
			if loader_par is not None:
				try:
					loader_par.val = par.eval()
				except Exception:
					pass

	def OnParPulse(self, par):
		"""Dispatch parameter pulse events to registered handlers."""
		if par.name in self.on_par_pulse_map:
			self.on_par_pulse_map[par.name]()
		elif self.loader_op is not None and getattr(self.loader_op, 'valid', True):
			loader_par = getattr(self.loader_op.par, par.name, None)
			if loader_par is not None and getattr(loader_par, 'isPulse', False):
				try:
					loader_par.pulse()
				except Exception:
					pass

	def onOutputto(self, value, prev):
		"""Handle Outputto parameter change — restart subprocess with new output mode."""
		self._log('Subprocess', f"Output mode changed: '{prev}' → '{value}', restarting subprocess...")
		self.close_subprocess()
		self.start_subprocess()

	def onPluginname(self, value, prev):
		"""Handle Pluginname parameter change.

		If cleared: unloads the current plugin and clears the builder.
		If set to a name with an existing CMakeLists.txt: loads the plugin
		project by reading the plugin type from the CMake header comment
		and creating the appropriate loader op chain.
		"""
		if value == '':
			self._log('Init', f"Plugin name cleared (was '{prev}'), clearing builder...")
			self.clear_plugin_builder()
		elif os.path.exists(self.CMakeListsPath):
			# Try to read plugin type from the CMakeLists.txt header comment
			# Format: # {'plugin_type': 'CHOP'}
			with open(self.CMakeListsPath, 'r') as f:
				first_line = f.readline()
			if first_line.startswith('#'):
				info = None
				try:
					info = ast.literal_eval(first_line[2:].strip())
				except Exception:
					pass
				if info is not None and isinstance(info, dict):
					plugin_type = info.get('plugin_type')
					if plugin_type is not None:
						self._log('Init', f"Loading existing PluginProject: '{self.Pluginname}' (type={plugin_type})")
						self.create_plugin_loader(plugin_type)
						self.start_subprocess()
						self.sync_custom_parameters(verbose=True)
						return

		# Fallback: just update loader reference and refresh DATs
		self.loader_op = self.ownerComp.op('plugin_loader')
		if self.loader_op is not None:
			self.loader_op.par.unloadplugin = True
			self.loader_op.cook(force=True)
			self._log('Init', f"Unloaded existing plugin loader for '{value}'")

		self.RefreshDats()

	# ========================================================================================== #
	#  FILE CHANGE CALLBACKS                                                                     #
	# ========================================================================================== #

	def OnPluginUpdate(self):
		"""Handle file changes in the build output directory (debounced).

		Called by the folder_bin DAT whenever any file changes in the
		build output directory. This fires for .obj files, .dll files,
		and other build artifacts.

		To avoid copying a stale DLL mid-build, this method debounces:
		each call cancels any previously scheduled copy and schedules
		a new one 10 frames later. This ensures rapid events (.obj
		compile → DLL link) collapse into a single copy of the final
		linked DLL.
		"""
		if self.loader_op is None or self.Pluginname == '':
			return

		build_path = self.build_path

		# Only react if the DLL exists — ignore .obj and other artifact changes
		if not os.path.exists(build_path):
			self._log('Build→Copy', f"DLL not yet produced ({build_path}), waiting for linker...")
			return

		# Cancel any previously scheduled copy — the latest trigger wins
		for r in runs:
			if r.group == 'copy_dll':
				r.kill()

		# Reset retry counter for a fresh build event
		self.open_attempts = 0

		# Record the DLL's current modification time to detect linker updates during debounce
		self._build_mtime = os.path.getmtime(build_path)

		self._log('Build→Copy', "File change detected, scheduling copy in 10 frames (debounce)...")
		run("args[0].ext.PluginBuilderExt._do_copy_plugin()", self.ownerComp, group='copy_dll', delayFrames=10)

	def _do_copy_plugin(self):
		"""Execute the actual DLL copy after the debounce delay.

		Copies the built DLL from the build output directory to the
		__Plugins__/ directory, then reloads it in the plugin_loader op.

		If files are still locked (e.g. linker hasn't released the DLL),
		retries up to 20 times with 5-frame delays between attempts.
		"""
		if self.loader_op is None or self.Pluginname == '':
			self._log('Build→Copy', f"Skipped: loader_op={self.loader_op}, Pluginname='{self.Pluginname}'")
			return

		self._log('Build→Copy', f"Debounce elapsed — executing copy for '{self.Pluginname}'")

		plugin_name = self.Pluginname
		build_path = self.build_path
		plugin_path = f"{self.plugin_dir}/{plugin_name}.dll"

		# --- Debug: source file info ---
		self._log('Build→Copy', f"build_path (source): {build_path}")
		self._log('Build→Copy', f"  exists: {os.path.exists(build_path)}")
		if os.path.exists(build_path):
			size = os.path.getsize(build_path)
			mtime = os.path.getmtime(build_path)
			locked = self.file_locked(build_path)
			self._log('Build→Copy', f"  size: {size} bytes")
			self._log('Build→Copy', f"  mtime: {mtime}")
			self._log('Build→Copy', f"  locked: {locked}")

			# Detect if the linker updated the DLL during our debounce wait
			expected_mtime = getattr(self, '_build_mtime', None)
			if expected_mtime is not None and mtime > expected_mtime:
				self._log('Build→Copy', f"  DLL was updated during debounce (mtime {expected_mtime} → {mtime}), using latest.")

		# --- Debug: destination file info ---
		self._log('Build→Copy', f"plugin_path (dest):  {plugin_path}")
		self._log('Build→Copy', f"  exists: {os.path.exists(plugin_path)}")
		if os.path.exists(plugin_path):
			self._log('Build→Copy', f"  size: {os.path.getsize(plugin_path)} bytes")
			self._log('Build→Copy', f"  locked: {self.file_locked(plugin_path)}")

		if not os.path.exists(build_path):
			self._log('Build→Copy', f"ERROR: Build output {build_path} does not exist. Aborting.")
			return

		# --- Unload plugin to release file locks ---
		self.loader_op.par.unloadplugin = True
		self.loader_op.cook(force=True)
		self._log('Build→Copy', "Unloaded plugin via plugin_loader")

		# --- Retry if files are still locked ---
		if self.file_locked(build_path) or self.file_locked(plugin_path):
			if self.open_attempts < 20:
				self.open_attempts += 1
				self._log('Build→Copy', f"File is locked (attempt {self.open_attempts}/20). Retrying in 5 frames...")
				run("args[0].ext.PluginBuilderExt._do_copy_plugin()", self.ownerComp, group='copy_dll', delayFrames=5)
				return
			else:
				self._log('Build→Copy', f"ERROR: File still locked after {self.open_attempts} attempts. Giving up.")
				self.open_attempts = 0
				return

		self.open_attempts = 0
		self._log('Build→Copy', "Files are unlocked, proceeding with copy.")

		# --- Ensure plugin output directory exists ---
		if not os.path.exists(self.plugin_dir):
			os.makedirs(self.plugin_dir)
			self._log('Build→Copy', f"Created plugin directory: {self.plugin_dir}")

		# --- Copy the DLL ---
		shutil.copyfile(build_path, plugin_path)
		self._log('Build→Copy', f"Copied {build_path} → {plugin_path}")

		# --- Verify and reload ---
		if os.path.exists(plugin_path):
			self._log('Build→Copy', f"  Verified: {plugin_path} ({os.path.getsize(plugin_path)} bytes)")
			self.loader_op.par.plugin = plugin_path
			self.loader_op.par.unloadplugin = False
			if hasattr(self.loader_op.par, 'reinitpulse'):
				self.loader_op.par.reinitpulse.pulse()
			self.loader_op.cook(force=True)
			self._log('Build→Copy', f"Plugin reloaded from {plugin_path}")
			run("args[0].ext.PluginBuilderExt.sync_custom_parameters(force_rebuild=True, verbose=True)", self.ownerComp, delayFrames=15)
		else:
			self._log('Build→Copy', f"ERROR: Copy failed, {plugin_path} does not exist after copy.")

	def OnSourceUpdate(self):
		"""Handle source file changes — triggers a recompile.

		Called by the folder_source DAT when any .cpp/.h file changes
		in the plugin's source directory.
		"""
		self._log('Source', f"Source file changed, recompiling '{self.Pluginname}'...")
		self.compile_plugin()

	def OnCMakeListsUpdate(self):
		"""Handle CMakeLists.txt changes — triggers a CMake reconfigure.

		Called by the CMakeLists DAT when CMakeLists.txt is modified.
		Re-runs the cmake configure step to regenerate Ninja build files.
		"""
		if not os.path.exists(self.CMakeListsPath):
			self._log('CMake', f"CMakeLists.txt not found at {self.CMakeListsPath}")
			return

		self._log('CMake', f"CMakeLists.txt changed, re-running CMake configure...")
		self.build_plugin()

	# ========================================================================================== #
	#  SUBPROCESS MANAGEMENT                                                                     #
	# ========================================================================================== #

	def start_subprocess(self):
		"""Start the persistent cmd.exe build subprocess.

		The subprocess runs vcvarsall.bat to initialize the MSVC environment,
		then stays open to receive cmake/ninja commands via stdin. Output
		can be directed to either TouchDesigner's text console (direct stdout)
		or captured to a queue for display in TD's textport.

		Returns:
			True if the subprocess started successfully, False if the
			working directory doesn't exist.
		"""
		# Bail if working directory doesn't exist yet
		if not os.path.exists(self.abs_working_dir):
			self._log('Subprocess', f"Cannot start — working dir does not exist: {self.abs_working_dir}")
			return False

		mode = self.ownerComp.par.Outputto.eval()
		cmd = self.start_subprocess_base_cmd
		self._log('Subprocess', f"Starting subprocess (mode={mode}, cwd={self.abs_working_dir})")

		if mode == 'TOUCH_TEXT_CONSOLE':
			# Direct output to TD's system console
			self.process = subprocess.Popen(
				cmd,
				stdin=subprocess.PIPE,
				text=True,
				shell=True,
				cwd=self.abs_working_dir
			)
		else:
			# Capture output to a queue for textport display
			self.process = subprocess.Popen(
				cmd,
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT,
				text=True,
				shell=True,
				bufsize=1,  # Line-buffered for real-time output
				cwd=self.abs_working_dir
			)

			self.queue = queue.Queue()
			self.output_thread = threading.Thread(target=self._output_reader, daemon=True)
			self.output_thread.start()

		started = self.process.returncode is None
		self._log('Subprocess', f"Subprocess {'started' if started else 'FAILED to start'} (pid={self.process.pid})")
		return started

	def _output_reader(self):
		"""Background thread: reads subprocess stdout line-by-line into the queue.

		Runs in a daemon thread. Each line from the subprocess is placed
		into self.queue for later retrieval by GetOutput()/PrintOutput().
		"""
		for line in self.process.stdout:
			self.queue.put(line)

	def SendCommand(self, command):
		"""Send a command string to the build subprocess.

		Writes the command followed by a newline to the subprocess's stdin
		and flushes to ensure it's sent immediately. If the subprocess is not
		running, attempts to start it first.

		Args:
			command: The shell command to execute (e.g. 'ninja -C build').

		Raises:
			RuntimeError: If the subprocess is not running and cannot be started.
		"""
		if self.process is None or self.process.poll() is not None:
			self._log('Subprocess', "Subprocess not active. Attempting to start process before sending command...")
			if not self.start_subprocess():
				raise RuntimeError("Build subprocess is not active and could not be started.")

		if self.process is not None and self.process.poll() is None:
			self.process.stdin.write(command + '\n')
			self.process.stdin.flush()
		else:
			raise RuntimeError("Subprocess is not running.")

	def CheckAndPrintOutput(self):
		"""Print any pending subprocess output (no-op if queue is empty)."""
		if self.queue is None or self.queue.empty():
			return
		self.PrintOutput()

	def GetOutput(self):
		"""Retrieve all pending output lines from the subprocess queue.

		Returns:
			List of output line strings. Empty if no output is pending.
		"""
		output_lines = []
		while not self.queue.empty():
			output_lines.append(self.queue.get_nowait())
		return output_lines

	def PrintOutput(self):
		"""Print all pending subprocess output to TD's textport."""
		output_lines = self.GetOutput()
		for line in output_lines:
			print(line, end='')

	def close_subprocess(self):
		"""Terminate the build subprocess and clean up resources.

		Closes stdin/stdout/stderr, terminates the process, and joins
		the output reader thread. Safe to call multiple times.
		"""
		if self.process is not None:
			self._log('Subprocess', f"Closing subprocess (pid={self.process.pid})...")

			if self.process.poll() is None:  # Still running
				if self.process.stdin is not None:
					self.process.stdin.close()
				if self.process.stdout is not None:
					self.process.stdout.close()
				if self.process.stderr is not None:
					self.process.stderr.close()

				self.process.terminate()
				self._log('Subprocess', "Process terminated.")
				del(self.process)
				self.process = None

		if hasattr(self, "output_thread") and self.output_thread.is_alive():
			self.output_thread.join()
			self._log('Subprocess', "Output reader thread joined.")