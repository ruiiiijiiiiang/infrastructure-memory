{
  description = "Workstation-local infrastructure memory with portable synchronized records";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          package = pkgs.callPackage ./default.nix { };
        in
        {
          default = package;
          infrastructure-memory = package;
        }
      );

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/infra-memory";
        };
      });

      checks = forAllSystems (pkgs: {
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      });

      devShells = forAllSystems (
        pkgs:
        let
          package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          python = pkgs.python3.withPackages (
            ps:
            package.dependencies
            ++ [
              ps.setuptools
              ps.ruff
            ]
          );
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.gitMinimal
            ];
          };
        }
      );

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
