FROM alpine:3
MAINTAINER Ruben de Groot <ruben@hacktor.com>

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=UTF-8

COPY requirements.txt /tmp/requirements.txt
RUN apk add --update --no-cache python3 py3-pip && ln -sf python3 /usr/bin/python && rm -f /var/cache/apk/*
RUN pip3 install --no-cache --upgrade pip setuptools wheel
RUN pip3 install -r /tmp/requirements.txt

RUN adduser -D charon
USER charon
WORKDIR /home/charon

COPY charon.py /home/charon/

CMD ["python", "--version"]

