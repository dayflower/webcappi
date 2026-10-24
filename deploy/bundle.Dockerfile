# Build from a staging directory containing default/ with bundle.json,
# user-data/ and extensions/. Preserve the unpacked extension's runtime path.
FROM busybox:1.37.0
COPY --chown=10001:10001 default/ /bundle/default/
USER 10001:10001
CMD ["sh", "-c", "cp -R /bundle/default /out/default"]
