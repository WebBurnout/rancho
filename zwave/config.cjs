// Z-Wave JS driver options, loaded by zwave-server --config.
// Keys come from the checkout's .env (systemd EnvironmentFile): 32 hex chars
// each. Losing them means re-pairing every secure device.

const key = (name) => {
  const value = process.env[name];
  if (!/^[0-9a-fA-F]{32}$/.test(value ?? "")) {
    throw new Error(`${name} must be 32 hex characters`);
  }
  return value;
};

module.exports = {
  securityKeys: {
    S0_Legacy: key("ZWAVE_S0_LEGACY"),
    S2_Unauthenticated: key("ZWAVE_S2_UNAUTHENTICATED"),
    S2_Authenticated: key("ZWAVE_S2_AUTHENTICATED"),
    S2_AccessControl: key("ZWAVE_S2_ACCESS_CONTROL"),
  },
  securityKeysLongRange: {
    S2_Authenticated: key("ZWAVE_LR_S2_AUTHENTICATED"),
    S2_AccessControl: key("ZWAVE_LR_S2_ACCESS_CONTROL"),
  },
  // Mexico uses the US 908 MHz band. 9 = "USA (Long Range)" in zwave-js's RFRegion.
  // Classic stays at the board's 0 dBm (zwave-js's US legal limit is -1 dBm).
  // Long Range may use up to 20 dBm in the US; the board ships capped at 14.
  rf: { region: 9, maxLongRangePowerlevel: 20 },
  storage: {
    cacheDir: __dirname + "/cache",
    // Values are written at most every 5 min: fewer SD card writes.
    throttle: "slow",
  },
  // To the journal only (volatile on this Pi).
  logConfig: {
    enabled: true,
    level: "info",
    logToFile: false,
    forceConsole: true,
  },
};
