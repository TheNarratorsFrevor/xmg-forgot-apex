"""D-Bus API definition shared by the daemon (xfad), the CLI (xfa) and the GNOME extension.

Bus name      dev.nulliche.xfa          (system bus in production)
Object path   /dev/nulliche/xfa
Interfaces    dev.nulliche.xfa.Daemon, dev.nulliche.xfa.Fan, dev.nulliche.xfa.Keyboard

Fan percentages are 0..100. Temperatures are degrees Celsius. Colors are 0..255 per channel.
"""

BUS_NAME = "dev.nulliche.xfa"
OBJECT_PATH = "/dev/nulliche/xfa"
IFACE_DAEMON = "dev.nulliche.xfa.Daemon"
IFACE_FAN = "dev.nulliche.xfa.Fan"
IFACE_KEYBOARD = "dev.nulliche.xfa.Keyboard"

FAN_MODES = ("auto", "curve", "manual")
FAN_SOURCES = ("max", "cpu", "gpu")

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
