# Loaded into CocoaPods on Linux (RUBYOPT=-r): the xcodeproj gem only reads
# XML/ASCII plists there, but XCFrameworks ship binary Info.plists.
begin
  require 'xcodeproj'
rescue LoadError
  return # e.g. while CocoaPods itself is being installed
end

module Xcodeproj
  module Plist
    class << self
      alias_method :xlinux_read_from_path, :read_from_path

      def read_from_path(path)
        return xlinux_read_from_path(path) unless File.binread(path, 8) == 'bplist00'
        xml = IO.popen(['python3', '-c', 'import plistlib,sys;sys.stdout.buffer.write(plistlib.dumps(plistlib.load(open(sys.argv[1],"rb"))))', path.to_s], &:read)
        tmp = "#{path}.xlinux.plist"
        File.write(tmp, xml)
        begin
          xlinux_read_from_path(tmp)
        ensure
          File.delete(tmp) if File.exist?(tmp)
        end
      end
    end
  end
end
