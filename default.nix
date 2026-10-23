{
  lib,
  python3Packages,
  gitMinimal,
  makeWrapper,
  fetchFromGitHub,
}:
let
  # The pinned nixpkgs has SDK 2.2 / httpx2 2.9.1. Keep this upgrade local
  # until it supplies SDK >=2.3 and the matching mcp-types / HTTP dependencies.
  httpSource = fetchFromGitHub {
    owner = "pydantic";
    repo = "httpx2";
    tag = "v2.10.0";
    hash = "sha256-Pcc2ykQzPnzzf3PHdzcdxD/bvHJeoWI0cmdefGrrJX4=";
  };
  httpcore2 = python3Packages.httpcore2.overridePythonAttrs {
    version = "2.10.0";
    src = httpSource;
  };
  httpx2 = (python3Packages.httpx2.override { inherit httpcore2; }).overridePythonAttrs {
    version = "2.10.0";
    src = httpSource;
    # 2.10 replaces certifi with truststore and pins httpcore2 to its own version.
    dependencies = with python3Packages; [
      anyio
      httpcore2
      idna
      truststore
    ];
  };
  mcp-types = python3Packages.mcp-types.override { mcp_2 = mcp; };
  blockbuster = python3Packages.blockbuster.overridePythonAttrs {
    version = "1.5.27";
    enabledTestPaths = [ "tests" ];
    src = fetchFromGitHub {
      owner = "cbornet";
      repo = "blockbuster";
      tag = "v1.5.27";
      hash = "sha256-r9UcyjBIZjnCxL/8RdMiGb5XiH0s8JnQ8fY9SIC4Hsw=";
    };
  };
  mcp = (python3Packages.mcp_2.override { inherit httpx2 mcp-types; }).overridePythonAttrs (old: {
    version = "2.3.0";
    src = fetchFromGitHub {
      owner = "modelcontextprotocol";
      repo = "python-sdk";
      tag = "v2.3.0";
      hash = "sha256-CnXi78zYxm0/R4fON/DzRYMSDYM/XM/9SE3s+Cf1BR0=";
    };
    nativeCheckInputs = old.nativeCheckInputs ++ [ blockbuster ];
  });
in
python3Packages.buildPythonApplication {
  pname = "infrastructure-memory";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ./.;
    filter =
      path: type:
      lib.cleanSourceFilter path type
      && !(lib.elem (builtins.baseNameOf path) [
        ".venv"
        ".direnv"
        ".ruff_cache"
        "__pycache__"
        "build"
        "dist"
        "result"
      ]);
  };
  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [
    cryptography
    numpy
    mcp
    onnxruntime
    tokenizers
    huggingface-hub
  ];
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ gitMinimal ];
  checkPhase = /* bash */ ''
    runHook preCheck
    python -B -m unittest discover -s tests -v
    runHook postCheck
  '';
  postFixup = /* bash */ ''
    wrapProgram $out/bin/infra-memory --prefix PATH : ${lib.makeBinPath [ gitMinimal ]}
  '';
  pythonImportsCheck = [ "infrastructure_memory" ];
  meta = {
    description = "Local infrastructure memory with Syncthing-portable records";
    mainProgram = "infra-memory";
    platforms = lib.platforms.linux;
  };
}
