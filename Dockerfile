# ---- Build ----
FROM public.ecr.aws/docker/library/node:18.16-alpine
COPY . .


# RUN npm install -g cnpm --registry=https://registry.npmmirror.com

RUN npm install -g cnpm --registry=https://registry.npmmirror.com
RUN npm config set registry https://mirrors.huaweicloud.com/repository/npm/
RUN cnpm config set registry https://mirrors.huaweicloud.com/repository/npm/
RUN cnpm install -g prebuild-install
# RUN cnpm install cmake-js
# RUN cnpm install && cnpm run build
RUN npm cache clean --force
RUN cnpm cache clean --force
RUN npm install --verbose && cnpm run build --verbose

# Expose the port the app will run on
EXPOSE 80

# Start the application
CMD ["npm", "start"]
