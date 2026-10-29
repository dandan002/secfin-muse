# Each mock's Postgres (the second container in its task): the official image plus a snapshot in S3,
# so a stopped system resumes where it left off. infra/up.sh builds it once per environment.
#
#   SNAPSHOT_URL=s3://<bucket>/<system>.sql.gz   restore on first boot, save on SIGTERM
#   (unset: plain Postgres, empty every start)
FROM public.ecr.aws/docker/library/postgres:16.6-alpine

RUN apk add --no-cache aws-cli

COPY restore.sh /docker-entrypoint-initdb.d/
COPY snapshot-entrypoint.sh /usr/local/bin/

ENTRYPOINT ["snapshot-entrypoint.sh"]
CMD ["postgres"]
