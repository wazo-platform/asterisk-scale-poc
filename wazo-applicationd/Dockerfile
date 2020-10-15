FROM python:3.7-slim-buster AS compile-image
LABEL maintainer="Wazo Maintainers <dev@wazo.community>"

RUN python -m venv /opt/venv
# Activate virtual env
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get -q update && apt-get -yq install gcc && apt install -yq git
WORKDIR /usr/src/
RUN git clone https://github.com/wazo-platform/wazo-applicationd
WORKDIR /usr/src/wazo-applicationd
RUN pip install -r requirements.txt
RUN python setup.py install

FROM python:3.7-slim-buster AS build-image
COPY --from=compile-image /opt/venv /opt/venv

# COPY ./etc/wazo-applicationd /etc/wazo-applicationd

RUN true \
    && apt-get -q update \
    && adduser --quiet --system --group --home /var/lib/wazo-calld wazo-applicationd \
    && mkdir -p /etc/wazo-applicationd/conf.d \
    && install -o www-data -g www-data /dev/null /var/log/wazo-applicationd.log \
    && rm -fr /var/lib/apt/lists/*

EXPOSE 9500

# Activate virtual env
ENV PATH="/opt/venv/bin:$PATH"
CMD ["wazo-applicationd", "-d"]
