vibekill __VERSION__ — offline install bundle
=============================================

Kills the vibe before your diff kills production.

This archive contains vibekill and every dependency it needs, as wheels. Nothing
here requires network access, which is the point: a plain wheel is not enough,
because pip would still try to resolve dependencies from PyPI.


INSTALL
-------

    sh install.sh                   # installs to ~/.local/vibekill
    sh install.sh /opt/vibekill     # or anywhere you can write

Then add it to your PATH:

    export PATH="$HOME/.local/vibekill/bin:$PATH"

Or install by hand into an environment you already have:

    pip install --no-index --find-links . vibekill


VERIFY
------

Confirm that vibekill genuinely cannot send anything anywhere:

    vibekill doctor

It attempts a connection to a public host and asserts that its own guard refuses
it. You should see:

    air-gap verification
      egress is blocked: a connection to example.com:443 was refused by the
      guard before any packet left the process


USE
---

    vibekill review                   review the last commit
    vibekill review --base main       review a branch against main
    vibekill review --uncommitted     review what you have not committed
    vibekill --help                   everything else

`vk` is a shorter alias for all of it. Reports are written to .vibekill/ as
review.md, review.html and review.json.

This works with no language model at all: deterministic analysers and built-in
checks produce a useful review on their own. Point it at a local model to add
the four reasoning agents:

    vibekill review --model qwen2.5-coder:7b


REQUIREMENTS
------------

Requires Python __PYTHON_VERSION__ on the target machine. The archive name
includes its Python ABI and platform. Dependencies that ship compiled wheels are
specific to both; download or build the bundle matching the target:

    vibekill-__VERSION__-offline-__BUNDLE_TAG__.tar.gz

    git clone <the repository> && cd vibekill
    make install
    make bundle          # CLI only
    make bundle-full     # including the Streamlit UI


WHAT IS IN HERE
---------------

    install.sh      the offline installer
    README.txt      this file
    *.whl           vibekill plus every transitive dependency

Licence: Apache-2.0.
