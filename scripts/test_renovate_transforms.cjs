const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const JSON5 = require("json5");
const jsonata = require("jsonata");

const root = path.resolve(__dirname, "..");
const config = JSON5.parse(
  fs.readFileSync(path.join(root, ".github/renovate.json5"), "utf8"),
);

async function transform(datasource, data) {
  const source = config.customDatasources[datasource];
  assert.ok(source, `missing custom datasource ${datasource}`);
  assert.equal(source.transformTemplates.length, 1);
  return jsonata(source.transformTemplates[0]).evaluate(data);
}

function plainJson(value) {
  return JSON.parse(JSON.stringify(value));
}

async function testAcliFormula() {
  const formula = fs.readFileSync(
    path.join(root, "tests/fixtures/renovate/atlassian-acli-formula.rb"),
    "utf8",
  );
  const lines = formula.split(/\r?\n/).map((line) => line.trim());
  const plainData = { releases: lines.map((version) => ({ version })) };
  const versionLine = lines.find((line) => /^version\s+"/.test(line));
  const expectedVersion = versionLine?.match(/^version\s+"([^"]+)"/)?.[1];
  assert.ok(expectedVersion, "fixture is missing its formula version");

  const versionResult = await transform("acli", plainData);
  assert.deepEqual(plainJson(versionResult.releases), [
    { version: expectedVersion },
  ]);

  for (const arch of ["amd64", "arm64"]) {
    const urlIndex = lines.findIndex(
      (line) => line.includes("/linux/") && line.includes(`linux_${arch}.tar.gz`),
    );
    assert.notEqual(urlIndex, -1, `fixture is missing the ${arch} Linux URL`);
    const expectedArtifactVersion = lines[urlIndex].match(/\/linux\/([^/]+)\//)?.[1];
    const expectedDigest = lines[urlIndex + 1].match(
      /sha256\s+"([0-9a-f]{64})"/,
    )?.[1];
    assert.ok(expectedArtifactVersion, `fixture is missing ${arch} version`);
    assert.ok(expectedDigest, `fixture is missing ${arch} digest`);

    const result = await transform(`acli-${arch}`, plainData);
    assert.deepEqual(plainJson(result.releases), [
      { version: expectedArtifactVersion, digest: expectedDigest },
    ]);
  }
}

async function main() {
  await testAcliFormula();
  console.log("Renovate acli JSONata datasource transforms match their fixture.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
