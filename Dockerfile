FROM golang:1.27.1 AS build
WORKDIR /src
COPY go.mod *.go ./
ARG BUILD_ID=development
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.buildID=${BUILD_ID}" -o /controller .

FROM scratch
COPY --from=build /controller /controller
USER 10001:10001
ENTRYPOINT ["/controller"]
