# Describe an Xcode project as JSON (for xlinux/core/xcode/project.py):
# targets, their build phases, script phases and build settings.
#
#   ruby xcodeproj_dump.rb <Project.xcodeproj> <Debug|Release>
#
# Runs on the Ruby that xlinux installs with CocoaPods (`xcodeproj` gem).
require 'json'
require 'xcodeproj'

project_path, configuration = ARGV
project = Xcodeproj::Project.open(project_path)

def settings_of(config)
  return {} unless config
  config.build_settings.transform_values { |v| v.is_a?(Array) ? v.join(' ') : v.to_s }
end

def xcconfig_of(config)
  ref = config && config.base_configuration_reference
  ref && File.expand_path(ref.real_path.to_s)
end

def files_of(phase)
  phase.files.map { |f| f.file_ref && [f, File.expand_path(f.file_ref.real_path.to_s)] }.compact
end

targets = project.targets.map do |t|
  config = t.build_configurations.find { |c| c.name == configuration }
  entry = {
    name: t.name,
    kind: t.isa,
    product_type: t.respond_to?(:product_type) ? t.product_type : nil,
    settings: settings_of(config),
    xcconfig: xcconfig_of(config),
    dependencies: t.dependencies.map { |d| d.target ? d.target.name : d.name }.compact,
    scripts: t.build_phases.grep(Xcodeproj::Project::Object::PBXShellScriptBuildPhase).map do |s|
      { name: s.name, script: s.shell_script }
    end,
  }
  if t.isa == 'PBXNativeTarget'
    entry[:sources] = files_of(t.source_build_phase).map do |f, path|
      { path: path, flags: (f.settings || {})['COMPILER_FLAGS'] }
    end
    entry[:headers] = files_of(t.headers_build_phase).map do |f, path|
      attrs = (f.settings || {})['ATTRIBUTES'] || []
      { path: path, visibility: attrs.include?('Public') ? 'public' : attrs.include?('Private') ? 'private' : 'project' }
    end
    entry[:resources] = files_of(t.resources_build_phase).map { |_, path| path }
  end
  entry
end

project_config = project.build_configurations.find { |c| c.name == configuration }
puts JSON.generate(
  project_dir: File.dirname(File.expand_path(project_path)),
  configuration: configuration,
  project_settings: settings_of(project_config),
  project_xcconfig: xcconfig_of(project_config),
  targets: targets,
)
