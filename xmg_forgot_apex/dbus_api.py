"""D-Bus API definition shared by the daemon (xfad), the CLI (xfa) and the GNOME extension.

Bus name      dev.nulliche.xfa          (system bus in production)
Object path   /dev/nulliche/xfa
Interfaces    dev.nulliche.xfa.Daemon, .Fan, .Keyboard, .Profile

Fan percentages are 0..100. Temperatures are degrees Celsius. Colors are 0..255 per channel.
"""

BUS_NAME = "dev.nulliche.xfa"
OBJECT_PATH = "/dev/nulliche/xfa"
IFACE_DAEMON = "dev.nulliche.xfa.Daemon"
IFACE_FAN = "dev.nulliche.xfa.Fan"
IFACE_KEYBOARD = "dev.nulliche.xfa.Keyboard"
IFACE_PROFILE = "dev.nulliche.xfa.Profile"

FAN_MODES = ("auto", "curve", "manual")
FAN_SOURCES = ("max", "cpu", "gpu")
PROFILES = ("power-saver", "balanced", "performance")

INTROSPECTION_XML = f"""
<node>
  <interface name="{IFACE_DAEMON}">
    <property name="Version" type="s" access="read"/>
    <property name="Board" type="s" access="read"/>
    <property name="Backend" type="s" access="read"/>
  </interface>
  <interface name="{IFACE_FAN}">
    <!-- "auto" (EC curve), "curve" (xfad curve), "manual" (fixed speeds) -->
    <property name="Mode" type="s" access="read"/>
    <!-- temperature the curve follows: "max", "cpu" or "gpu" -->
    <property name="Source" type="s" access="read"/>
    <!-- (temperature, percent) points, sorted by temperature -->
    <property name="Curve" type="a(ii)" access="read"/>
    <!-- percent for fan 0 (CPU side) and fan 1 (GPU side) used in manual mode -->
    <property name="ManualSpeed" type="ai" access="read"/>
    <!-- live readings, refreshed every poll interval -->
    <property name="Temps" type="ai" access="read"/>
    <property name="Duty" type="ai" access="read"/>
    <!-- percent the curve currently asks for (curve mode), else -1 -->
    <property name="CurveTarget" type="i" access="read"/>
    <method name="SetMode"><arg name="mode" type="s" direction="in"/></method>
    <method name="SetManual">
      <arg name="fan0" type="i" direction="in"/><arg name="fan1" type="i" direction="in"/>
    </method>
    <method name="SetCurve"><arg name="points" type="a(ii)" direction="in"/></method>
    <method name="SetSource"><arg name="source" type="s" direction="in"/></method>
  </interface>
  <interface name="{IFACE_PROFILE}">
    <!-- Performance profiles. Names match power-profiles-daemon (GNOME "Power Mode"):
         power-saver, balanced, performance. Each profile has its own fan settings; the
         Fan interface always operates on the active profile. The EC mode register follows
         the active profile and the hardware mode button changes it (xfad polls for that). -->
    <property name="ActiveProfile" type="s" access="read"/>
    <property name="Profiles" type="as" access="read"/>
    <!-- raw EC register 0x0751 -->
    <property name="EcMode" type="i" access="read"/>
    <!-- keep power-profiles-daemon's ActiveProfile and ours in sync (both directions) -->
    <property name="SyncPowerProfiles" type="b" access="read"/>
    <!-- fan curve preset names (from TUXEDO Control Center) -->
    <property name="Presets" type="as" access="read"/>
    <method name="SetActiveProfile"><arg name="name" type="s" direction="in"/></method>
    <method name="SetSyncPowerProfiles"><arg name="enabled" type="b" direction="in"/></method>
    <!-- JSON of one profile: fan.mode, fan.source, fan.curve, fan.manual -->
    <method name="GetProfile"><arg name="name" type="s" direction="in"/><arg name="json" type="s" direction="out"/></method>
    <method name="SetProfile"><arg name="name" type="s" direction="in"/><arg name="json" type="s" direction="in"/></method>
    <!-- load a preset's CPU curve into the active profile (and switch it to curve mode) -->
    <method name="ApplyPreset"><arg name="preset" type="s" direction="in"/></method>
  </interface>
  <interface name="{IFACE_KEYBOARD}">
    <property name="Available" type="b" access="read"/>
    <property name="LedCount" type="i" access="read"/>
    <property name="Brightness" type="i" access="read"/>
    <property name="MaxBrightness" type="i" access="read"/>
    <!-- r, g, b of the whole keyboard (0..255) -->
    <property name="Color" type="ai" access="read"/>
    <method name="SetBrightness"><arg name="value" type="i" direction="in"/></method>
    <method name="SetColor">
      <arg name="r" type="i" direction="in"/><arg name="g" type="i" direction="in"/><arg name="b" type="i" direction="in"/>
    </method>
    <!-- per-key: (led index, r, g, b) -->
    <method name="SetKeyColors"><arg name="entries" type="a(iiii)" direction="in"/></method>
  </interface>
</node>
"""
