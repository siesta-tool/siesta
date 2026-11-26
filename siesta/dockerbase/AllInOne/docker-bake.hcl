# Multi-architecture build of the all-in-one image. Run from the repository root,
# normally through build.sh:
#   docker buildx bake -f siesta/dockerbase/AllInOne/docker-bake.hcl [--push | --load]
#
# The Spark base (siesta/dockerbase/Spark/Spark.dockerfile) is built as a dependency
# and fed straight into AllInOne.dockerfile, so it is never tagged or pulled (the
# Docker Hub image siesta/spark is unrelated and broken).

variable "TAGS" {
  default = "balaktsis/siesta:latest"
}

variable "PLATFORMS" {
  default = "linux/amd64,linux/arm64"
}

variable "MINIO_IMAGE" {
  default = "chainguard/minio:latest"
}

variable "REVISION" {
  default = "unknown"
}

group "default" {
  targets = ["siesta"]
}

target "spark-base" {
  context    = "siesta/dockerbase/Spark"
  dockerfile = "Spark.dockerfile"
  platforms  = split(",", PLATFORMS)
}

target "siesta" {
  context    = "."
  dockerfile = "siesta/dockerbase/AllInOne/AllInOne.dockerfile"
  contexts = {
    spark-base = "target:spark-base"
  }
  args = {
    BASE_IMAGE  = "spark-base"
    MINIO_IMAGE = MINIO_IMAGE
  }
  platforms = split(",", PLATFORMS)
  tags      = split(",", TAGS)
  labels = {
    "org.opencontainers.image.source"   = "https://hub.docker.com/r/balaktsis/siesta"
    "org.opencontainers.image.revision" = REVISION
  }
}
