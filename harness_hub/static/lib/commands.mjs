// Every `harness` command line the pages print, in one table (#251). The [daemon] contract check
// (tests/contract/test_host_commands.py) parses each one with the daemon's own CLI parser, and no other static file
// writes a `harness` command (tests/test_host_command_sources.py).
//
// `<name>` is one word: the owner fills it in, or the page fills it with hostCommand(). `[flag: text]` adds " text"
// only when the page passes a truthy `flag`; the check parses the line with and without each one.
export const COMMANDS = Object.freeze({
  hubStatus: "harness hub status",
  hubApprove: "harness hub approve <id> --match <code>",
  hubDeny: "harness hub deny <id>",
  hubRelease: "harness hub release --confirm",
  hubEntriesValidate: "harness hub entries validate",
  hubEntriesInit: "harness hub entries init",
  keysList: "harness keys list",
  keysRevoke: "harness keys revoke <key_id>",
  appsSetLimits: "harness apps set-limits <id> --max-running <integer> --max-queued <integer>",
  appsClearLimits: "harness apps set-limits <id> --set max_running=null --set max_queued=null",
  appsRetention: "harness apps retention <id> --retention-days <days>",
  appsClearRetention: "harness apps retention <id> --set retention_days=null",
  appsRestore: "harness apps restore <id>",
  pairingList: "harness pairing-requests list",
  // The daemon refuses an arm or approve with an elevated scope unless it carries --acknowledge-elevated (#91).
  pairingArm: "harness pairing-requests arm <catalog_app_id> --scopes <scopes>[origin: --origin <origin>]" +
    "[elevated: --acknowledge-elevated]",
  pairingApprove: "harness pairing-requests approve <id> --match <code>[elevated: --acknowledge-elevated]",
  pairingConfirm: "harness pairing-requests confirm <id> --match <code>",
  pairingDeny: "harness pairing-requests deny <id>",
  configSchema: "harness config schema",
  configShow: "harness config show",
  configValidate: "harness config validate <key>=<value>",
  configSet: "harness config set <key>=<value>",
  configRollback: "harness config rollback --confirm",
  configRestart: "harness config restart --confirm",
  smartApprovalsShow: "harness smart-approvals show",
  smartApprovalsSet: "harness smart-approvals set <mode>",
  backendsList: "harness backends list",
  backendsSet: "harness backends set <name> --model <model> --effort <effort>",
  memoryShow: "harness memory show",
  memorySetProfile: "harness memory set-profile <content>",
  githubAuthShow: "harness github-member-auth show",
  githubAuthSet: "harness github-member-auth set <enabled>",
  googleSigninStatus: "harness google-signin status",
  notifyTest: "harness notify test",
});

const SEGMENT = /\[(\w+):( [^\]]*)\]/g;
const PLACEHOLDER = /<([A-Za-z_][\w.-]*)>/g;

function template(name) {
  if (!Object.hasOwn(COMMANDS, name)) throw new Error(`No host command named ${name}`);
  return COMMANDS[name];
}

// The command `name` with each placeholder in `values` filled in (the rest stay as `<name>` for the owner) and each
// `[flag: ...]` segment kept when values[flag] is truthy. A filled-in value is never read as a placeholder.
export function hostCommand(name, values = {}) {
  return template(name).replace(SEGMENT, (_, flag, text) => (values[flag] ? text : ""))
    .replace(PLACEHOLDER, (whole, key) => (Object.hasOwn(values, key) ? String(values[key]) : whole));
}

// The command words alone, for prose that names a command: "harness hub approve".
export function commandWords(name) {
  const words = [];
  for (const word of template(name).split(" ")) {
    if (/^[<[-]/.test(word)) break;
    words.push(word);
  }
  return words.join(" ");
}
