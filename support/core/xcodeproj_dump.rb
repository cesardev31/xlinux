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

# Xcode 16+ "synchronized" folders: every file under them belongs to the
# target, except the target's membership exceptions; nothing is listed in
# the build phases.
SOURCE_EXTENSIONS = %w[.swift .m .mm .c .cpp .cc .cxx].freeze
IGNORED_EXTENSIONS = %w[.h .hh .hpp .entitlements .xcconfig .modulemap .md .docc .xctestplan .pch].freeze
# Folders Xcode handles as one item.
PACKAGE_EXTENSIONS = %w[.xcassets .bundle .lproj .xcdatamodeld .xcdatamodel .scnassets .framework
                        .xcframework .docc .playground .icon].freeze

def synchronized_files(target)
  sources, resources = [], []
  groups = target.respond_to?(:file_system_synchronized_groups) ? target.file_system_synchronized_groups : []
  groups.each do |group|
    root = File.expand_path(Xcodeproj::Project::Object::GroupableHelper.real_path(group).to_s)
    excluded, flags = [], {}
    (group.exceptions || []).each do |e|
      mine = e.respond_to?(:target) ? e.target == target : target.build_phases.include?(e.build_phase)
      next unless mine
      excluded += e.membership_exceptions || []
      flags.merge!(e.additional_compiler_flags_by_relative_path || {}) if e.respond_to?(:additional_compiler_flags_by_relative_path)
    end
    folders = group.explicit_folders || []
    walk = lambda do |dir, rel|
      Dir.children(dir).sort.each do |name|
        next if name.start_with?('.')
        path, relative = File.join(dir, name), (rel ? File.join(rel, name) : name)
        next if excluded.include?(relative)
        ext = File.extname(name).downcase
        if File.directory?(path) && !folders.include?(relative) && !PACKAGE_EXTENSIONS.include?(ext)
          walk.call(path, relative)
        elsif SOURCE_EXTENSIONS.include?(ext) && !File.directory?(path)
          sources << { path: path, flags: flags[relative] }
        elsif !IGNORED_EXTENSIONS.include?(ext) && name != 'Info.plist'
          resources << path
        end
      end
    end
    walk.call(root, nil) if File.directory?(root)
  end
  [sources, resources]
end

targets = project.targets.map do |t|
  config = t.build_configurations.find { |c| c.name == configuration }
  entry = {
    name: t.name,
    configuration_available: !config.nil?,
    kind: t.isa,
    product_type: t.respond_to?(:product_type) ? t.product_type : nil,
    settings: settings_of(config),
    xcconfig: xcconfig_of(config),
    dependencies: t.dependencies.map { |d| d.target ? d.target.name : d.name }.compact,
    # Targets whose product a Copy Files phase puts in PlugIns (dstSubfolderSpec 13):
    # the extensions this app embeds ("Embed Foundation Extensions").
    embedded_extensions: t.build_phases.grep(Xcodeproj::Project::Object::PBXCopyFilesBuildPhase)
      .select { |ph| ph.dst_subfolder_spec.to_s == '13' }
      .flat_map { |ph| ph.files.map { |f| f.file_ref } }
      .map { |ref| project.targets.find { |x| x.respond_to?(:product_reference) && x.product_reference == ref } }
      .compact.map(&:name),
    packages: t.respond_to?(:package_product_dependencies) ? t.package_product_dependencies.map(&:product_name) : [],
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
    synced_sources, synced_resources = synchronized_files(t)
    entry[:sources] += synced_sources
    entry[:resources] += synced_resources
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
