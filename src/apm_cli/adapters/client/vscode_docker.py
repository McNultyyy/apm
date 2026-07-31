"""``docker run`` argument assembly for the VS Code MCP adapter (#2385).

Split out of ``vscode`` so that adapter stays inside the module line budget
(#1078). Both entry points take the adapter class so the shared docker
helpers on ``MCPClientAdapter`` keep their single canonical owner.
"""

from ...core.docker_args import DockerArgsProcessor


def docker_run_args(cls, package: dict, runtime_vars: dict | None = None) -> list[str] | None:
    """Build ``docker run`` arguments from a container package's metadata.

    ``_extract_package_args`` cannot serve this path: the npm, pypi, and
    generic branches read an empty extraction as "synthesize the command
    from the package name", so teaching it the MCP Registry v0.1 argument
    shape would strip the package name out of those launchers. Container
    packages instead need the registry's run options assembled into a
    launchable command, which is what this builds.

    Handles both argument spellings -- v0.1 ``value`` (beside a ``type``)
    and the legacy ``value_hint`` -- because reading only the latter
    dropped every option a v0.1 registry supplied, including mounts whose
    values APM had just collected from the user (#2377). ``named`` entries
    contribute their flag as well as its value, matching
    ``CopilotClientAdapter._process_arguments``.

    ``package_arguments`` are the container's own argv and follow the image
    per ``docker run [OPTIONS] IMAGE [ARG...]``.

    Returns ``None`` -- leaving the caller on its previous synthesized
    launcher -- when the registry arguments cannot form a usable command:
    no ``run`` verb, or a placeholder in a required entry that this
    install could not resolve. Emitting a half-substituted mount path
    would be worse than the image-only command that shipped before. An
    *optional* entry whose placeholder is unresolved is dropped on its
    own instead -- declining a whole working command over a mount the
    registry itself marked optional would throw away every required
    argument with it.

    Args:
        package (dict): A single package entry from the registry.
        runtime_vars (dict, optional): Collected runtime variable values.

    Returns:
        list[str] | None: Ordered ``docker`` arguments, or None to decline.
    """
    if not package:
        return None
    run_options = docker_arg_values(cls, package.get("runtime_arguments"), runtime_vars)
    if run_options is None:
        return None
    package_args = docker_arg_values(cls, package.get("package_arguments"), runtime_vars)
    if package_args is None:
        return None
    if not run_options and package_args and package_args[0] == "run":
        image_index = cls._docker_image_arg_index(package_args, package.get("name"))
        if image_index is None:
            run_options = package_args
            package_args = []
        else:
            run_options = package_args[: image_index + 1]
            package_args = package_args[image_index + 1 :]
    elif not run_options and package_args:
        run_options = ["run", "-i", "--rm"]
    # The subcommand has to lead: `docker -v … run` is not a run invocation,
    # and there would be nothing for the flag normalizer to anchor on.
    if not run_options or run_options[0] != "run":
        return None

    image = package.get("name")
    # Some registry data repeats the whole docker argv in package_arguments
    # instead of describing the container's own arguments. Appending that
    # after the image would hand docker a second `run …` as the container
    # command, so treat it as a duplicate of what runtime_arguments said.
    if (package_args and package_args[0] == "run") or any(
        cls._docker_image_references_match(argument, image) for argument in package_args
    ):
        package_args = []

    # Reuse the shared normalizer so a registry template that omits -i/--rm
    # still yields an interactive, self-cleaning container.
    args: list[str] = DockerArgsProcessor.process_docker_args(run_options, {})
    try:
        with_image: list[str] = cls._ensure_docker_image_arg(args, image)
    except ValueError:
        return None
    return with_image + package_args


def docker_arg_values(cls, entries: list | None, runtime_vars: dict | None) -> list[str] | None:
    """Resolve one registry argument list into CLI argument strings.

    Returns ``None`` when a *required* entry carries a placeholder this
    install has no value for, so the caller can decline the whole command.
    An optional entry in the same situation is skipped by itself.

    An optional entry whose placeholder DID resolve is kept: its value was
    explicitly collected from the user, and discarding user-supplied
    configuration is the failure mode #2377 exists to prevent. (This also
    matches the legacy ``value_hint`` walk in ``_extract_package_args``,
    which never gated variables-bearing entries on ``is_required``.)
    """
    args: list[str] = []
    for arg in entries or []:
        if not isinstance(arg, dict):
            continue
        arg_type = arg.get("type")
        # v0.1 marks optional entries with ``isRequired``; only package-level
        # keys get camelCase-normalized, so accept either spelling here. An
        # explicit null means "unspecified", not "optional".
        required = arg.get("is_required", arg.get("isRequired"))
        required = True if required is None else bool(required)
        if not required and "variables" not in arg:
            continue

        # A typed entry carries its payload in ``value`` -- ``value_hint`` is
        # the schema's *display* hint for it, and emitting that renders
        # placeholder words like "env_var_name" as CLI arguments. Untyped
        # legacy entries have only the hint. This mirrors
        # ``CopilotClientAdapter._process_arguments``.
        if arg_type in ("positional", "named"):
            raw = arg.get("value", arg.get("default"))
        else:
            raw = arg.get("value_hint", arg.get("value", arg.get("default")))

        if raw is None or raw == "":
            # A named entry contributes its flag even with no value:
            # dropping ``--read-only`` silently weakens the container.
            if arg_type == "named" and arg.get("name"):
                name = str(arg["name"])
                if cls._docker_option_requires_value(name):
                    if required:
                        return None
                    continue
                args.append(name)
            continue

        template = str(raw)
        variables = arg.get("variables")
        if isinstance(variables, dict) and variables:
            substituted = cls._substitute_runtime_variables(template, variables, runtime_vars)
            if substituted is None:
                if required:
                    return None
                continue
            template = substituted
        if arg_type == "named" and arg.get("name"):
            args.append(str(arg["name"]))
        args.append(template)
    return args
