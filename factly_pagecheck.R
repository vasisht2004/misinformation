library(rvest)

for(i in 1:250){
  
  url <- if(i == 1){
    "https://factly.in/category/english/"
  } else {
    paste0("https://factly.in/category/english/page/", i, "/")
  }
  
  page <- tryCatch(read_html(url), error = function(e) NULL)
  
  if(is.null(page)) break
  
  first_link <- page %>%
    html_elements("h2.entry-title a, h3.entry-title a, h2 a") %>%
    html_attr("href") %>%
    .[1]
  
  article <- read_html(first_link)
  
  date <- article %>%
    html_elements("time") %>%
    html_attr("datetime") %>%
    .[5]        # publication date
  
  cat("Page", i, ":", substr(date, 1, 10), "\n")
}
library(rvest)

page <- read_html("https://factly.in/category/english/page/80/")



library(rvest)
library(stringr)

for (i in seq(1, 250, by = 10)) {
  
  url <- if (i == 1) {
    "https://factly.in/category/english/"
  } else {
    paste0("https://factly.in/category/english/page/", i, "/")
  }
  
  page <- tryCatch(read_html(url), error = function(e) NULL)
  if (is.null(page)) next
  
  first_link <- page %>%
    html_elements("h2.entry-title a, h3.entry-title a, h2 a") %>%
    html_attr("href") %>%
    .[1]
  
  article <- read_html(first_link)
  
  html <- as.character(article)
  
  date <- str_extract(
    html,
    '"datePublished":"\\d{4}-\\d{2}-\\d{2}'
  )
  
  date <- str_remove(date, '"datePublished":"')
  
  cat("Page", i, ":", date, "\n")
}








html_elements(article, 'meta[property="article:published_time"]') %>%
  html_attr("content")





# First article link
first_link <- page %>%
  html_elements("h2.entry-title a, h3.entry-title a, h2 a") %>%
  html_attr("href") %>%
  .[1]

first_link

article <- read_html(first_link)

html_elements(article, "time") %>%
  html_text(trim = TRUE)
html_text(article) %>% grep("2024", ., value = TRUE)


library(stringr)

html <- as.character(article)

date <- str_extract(
  html,
  '"datePublished":"\\d{4}-\\d{2}-\\d{2}'
)

date <- str_remove(date, '"datePublished":"')

year <- substr(date, 1, 4)
date
year
